"""Draft config/market_map.csv by matching each Cup race to a Kalshi
contract and a Polymarket outcome, deterministically (title/keyword
matching, no LLM - CLAUDE.md Hard Rule 2). Every row is written with
verified=False; a human must hand-check each launch market before it's
trusted for fair value (docs/PLAN.md Stage 1 step 4, LAUNCH_CHECKLIST.md).

    python -m scripts.draft_market_map [--markets data/cup_markets.csv]

Kalshi: public API, no auth (docs/kalshi/ not yet saved locally - endpoints
discovered live and used as observed, not guessed):
  https://api.elections.kalshi.com/trade-api/v2/series?category=Elections
  https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=...
Polymarket: public Gamma API, no auth (CLAUDE.md architecture doc):
  https://gamma-api.polymarket.com/public-search?q=...

Both are read-only market data reads (CLAUDE.md Hard Rule 5).
"""

from __future__ import annotations

import argparse
import csv
import random
import re
import sys
import time
from collections import defaultdict

import httpx

from predcup.cup_markets import STATE_ABBREVIATIONS
from predcup.market_map import ExternalMarket, build_row, match_party

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_BASE = "https://gamma-api.polymarket.com"
REQUEST_DELAY_SECONDS = 0.25
MAX_RETRIES = 5

ABBR_TO_STATE = {v: k for k, v in STATE_ABBREVIATIONS.items()}


def _get_with_retry(client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    for attempt in range(MAX_RETRIES):
        resp = client.get(url, **kwargs)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp
        time.sleep((2**attempt) + random.uniform(0, 1))
    resp.raise_for_status()
    return resp


# --- Kalshi -------------------------------------------------------------


def fetch_kalshi_elections_series(client: httpx.Client) -> list[dict]:
    all_series: list[dict] = []
    cursor = ""
    while True:
        params = {"category": "Elections", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = _get_with_retry(client, f"{KALSHI_BASE}/series", params=params)
        data = resp.json()
        all_series.extend(data["series"])
        cursor = data.get("cursor", "")
        time.sleep(REQUEST_DELAY_SECONDS)
        if not cursor or not data["series"]:
            break
    return all_series


def _find_state_in_text(text: str) -> str | None:
    for name in sorted(STATE_ABBREVIATIONS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
            return STATE_ABBREVIATIONS[name]
    return None


def build_kalshi_state_index(series: list[dict]) -> tuple[dict, dict]:
    """state -> [series ticker] for Governor and Senate PARTY-tagged series."""
    gov: dict[str, list[str]] = defaultdict(list)
    sen: dict[str, list[str]] = defaultdict(list)
    for s in series:
        ticker = s["ticker"].upper()
        if "PARTY" not in ticker:
            continue
        state = _find_state_in_text(s["title"])
        if not state:
            continue
        if ticker.startswith("GOVPARTY") or ticker.startswith("KXGOVPARTY"):
            gov[state].append(s["ticker"])
        elif ticker.startswith("SENATEPARTY") or ticker.startswith("KXSENATEPARTY"):
            sen[state].append(s["ticker"])
    return gov, sen


def _pick_canonical_series(tickers: list[str]) -> str:
    non_special = [t for t in tickers if "SPECIAL" not in t.upper() and not t.upper().endswith("S")]
    pool = non_special or tickers
    return sorted(pool, key=len)[0]


def fetch_kalshi_series_markets(client: httpx.Client, series_ticker: str) -> list[dict]:
    resp = _get_with_retry(
        client, f"{KALSHI_BASE}/markets", params={"series_ticker": series_ticker, "limit": 20}
    )
    time.sleep(REQUEST_DELAY_SECONDS)
    return resp.json().get("markets", [])


def kalshi_match_for_race(
    client: httpx.Client, office: str, state: str, gov_index: dict, sen_index: dict
) -> tuple[list[ExternalMarket], float, str]:
    if office == "Governor" and state in gov_index:
        series = _pick_canonical_series(gov_index[state])
    elif office == "Senate" and state in sen_index:
        series = _pick_canonical_series(sen_index[state])
    elif state == "US" and office == "Senate":
        series = "CONTROLS"
    elif state == "US" and office == "House":
        series = "CONTROLH"
    else:
        return [], 0.0, "no Kalshi party-level series identified for this race"

    markets = fetch_kalshi_series_markets(client, series)
    if series in ("CONTROLS", "CONTROLH"):
        # These series span multiple election cycles (e.g. CONTROLS-2026-R
        # *and* CONTROLS-2028-R both exist) -- the Cup's chamber-control
        # races are about 2026, so anything else must be filtered out
        # explicitly rather than taking whichever sorts first.
        markets = [m for m in markets if "2026" in m["ticker"]]
    if not markets:
        return [], 0.1, f"Kalshi series {series} identified but has no markets populated yet"

    externals = [ExternalMarket(ref=m["ticker"], text=m["title"]) for m in markets]
    return externals, 0.9, f"Kalshi series {series}"


# --- Polymarket -----------------------------------------------------------


def polymarket_search(client: httpx.Client, query: str) -> list[dict]:
    resp = _get_with_retry(
        client, f"{POLY_BASE}/public-search", params={"q": query, "limit_per_type": 5}
    )
    time.sleep(REQUEST_DELAY_SECONDS)
    return resp.json().get("events", [])


def _query_for_race(office: str, state: str, district: str) -> str:
    if state == "US":
        return f"{office} control 2026"
    state_name = ABBR_TO_STATE.get(state, state)
    if office == "House":
        return f"{state_name} {district} House 2026"
    return f"{state_name} {office} 2026"


_NOT_GENERAL_ELECTION = ("primary", "nominee", "runoff", "caucus")


def poly_match_for_race(
    client: httpx.Client, office: str, state: str, district: str
) -> tuple[list[ExternalMarket], float, str]:
    query = _query_for_race(office, state, district)
    events = polymarket_search(client, query)
    if not events:
        return [], 0.0, f"no Polymarket event found for query {query!r}"

    state_name = ABBR_TO_STATE.get(state, "United States" if state == "US" else state)
    best = None
    for ev in events:
        title = ev.get("title", "")
        title_lower = title.lower()
        # We want the general-election party-outcome market, not an
        # intra-party primary/nominee contest -- both mention the state,
        # office and often "Republican"/"Democrat" too, so this must be
        # filtered explicitly rather than relying on keyword presence.
        if any(kw in title_lower for kw in _NOT_GENERAL_ELECTION):
            continue
        if state == "US":
            if office.lower() in title_lower:
                best = ev
                break
        elif state_name.lower() in title_lower and office.lower() in title_lower:
            best = ev
            break
    if best is None:
        return [], 0.2, f"Polymarket search {query!r} returned no confident title match"

    sub_markets = best.get("markets", [])
    externals = []
    for m in sub_markets:
        clob_ids_raw = m.get("clobTokenIds")
        if not clob_ids_raw:
            continue
        yes_token = clob_ids_raw[0] if isinstance(clob_ids_raw, list) else None
        if yes_token is None:
            import json as _json

            try:
                yes_token = _json.loads(clob_ids_raw)[0]
            except (ValueError, IndexError, TypeError):
                continue
        externals.append(ExternalMarket(ref=yes_token, text=m.get("question", "")))
    return externals, 0.7, f"Polymarket event {best.get('slug', '')}"


# --- Orchestration --------------------------------------------------------


def load_markets(csv_path: str) -> list[dict[str, str]]:
    with open(csv_path, newline="") as f:
        return list(csv.DictReader(f))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markets", default="data/cup_markets.csv")
    parser.add_argument("--output", default="config/market_map.csv")
    args = parser.parse_args()

    markets = load_markets(args.markets)

    with httpx.Client(timeout=15) as client:
        print("Fetching Kalshi Elections series index...", file=sys.stderr)
        series = fetch_kalshi_elections_series(client)
        gov_index, sen_index = build_kalshi_state_index(series)
        print(f"Kalshi: {len(gov_index)} governor states, {len(sen_index)} senate states indexed", file=sys.stderr)

        # Cache per-race external market lookups so each race's several
        # SIG markets (R/D/I) share one Kalshi + one Polymarket call.
        kalshi_cache: dict[str, tuple[list[ExternalMarket], float, str]] = {}
        poly_cache: dict[str, tuple[list[ExternalMarket], float, str]] = {}

        rows = []
        for i, m in enumerate(markets):
            race_key = m["race_key"]
            office, state, district, party = m["office"], m["state"], m["district"], m["party"]

            if race_key not in kalshi_cache:
                kalshi_cache[race_key] = kalshi_match_for_race(client, office, state, gov_index, sen_index)
            if race_key not in poly_cache:
                poly_cache[race_key] = poly_match_for_race(client, office, state, district)

            kalshi_markets, kalshi_conf, kalshi_note = kalshi_cache[race_key]
            poly_markets, poly_conf, poly_note = poly_cache[race_key]

            kalshi_hit = match_party(kalshi_markets, party)
            poly_hit = match_party(poly_markets, party)

            row = build_row(
                platform_id=m["id"],
                kalshi_ref=kalshi_hit.ref if kalshi_hit else None,
                kalshi_confidence=kalshi_conf if kalshi_hit else 0.0,
                kalshi_note="" if kalshi_hit else kalshi_note,
                poly_ref=poly_hit.ref if poly_hit else None,
                poly_confidence=poly_conf if poly_hit else 0.0,
                poly_note="" if poly_hit else poly_note,
            )
            rows.append(row)
            if (i + 1) % 20 == 0:
                print(f"  matched {i + 1}/{len(markets)} markets", file=sys.stderr)

    fieldnames = [
        "platform_id", "kalshi_ticker", "poly_token_id", "polarity",
        "rule_diff_notes", "confidence", "verified", "tier",
    ]  # fmt: skip
    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "platform_id": row.platform_id,
                    "kalshi_ticker": row.kalshi_ticker,
                    "poly_token_id": row.poly_token_id,
                    "polarity": row.polarity,
                    "rule_diff_notes": row.rule_diff_notes,
                    "confidence": row.confidence,
                    "verified": "false",
                    "tier": "",
                }
            )

    print(f"Wrote {len(rows)} rows to {args.output}. Nothing marked verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
