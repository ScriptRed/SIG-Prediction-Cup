"""Draft config/market_map.csv by matching each Cup race to a Kalshi
contract and a Polymarket outcome, deterministically (title/keyword
matching, no LLM - CLAUDE.md Hard Rule 2). Every row is written with
verified=False; a human must hand-check each launch market before it's
trusted for fair value (docs/PLAN.md Stage 1 step 4, LAUNCH_CHECKLIST.md).

    python -m scripts.draft_market_map [--markets data/cup_markets.csv] [--offices Senate]

--offices redrafts only those offices' rows; every other row is copied
from the existing map unchanged. A row already verified=true is never
redrafted.

Kalshi: public API, no auth (docs/kalshi/openapi.yaml):
  https://api.elections.kalshi.com/trade-api/v2/series?category=Elections
  https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=...
  https://api.elections.kalshi.com/trade-api/v2/events?series_ticker=...&with_nested_markets=true
Governors match GOVPARTY<ST> series by party keyword; state Senate races
match SENATE<ST>-26 events by event title (predcup.market_map, Kalshi
Senate section).
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
from predcup.market_map import (
    ExternalMarket,
    build_row,
    index_senate_events,
    match_party,
    match_senate_party,
)

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


# Series that can hold a state Senate general election: SENATE<ST>, the
# special-election SENATE<ST>S, and KX-prefixed ones (KXSENATELA). Two-letter
# codes only, so the KXSENATE<ST>D/R nominee series are not fetched.
_SENATE_SERIES_RE = re.compile(r"^(KX)?SENATE[A-Z]{2}S?$")


def fetch_kalshi_senate_events(client: httpx.Client, series: list[dict]) -> list[dict]:
    events: list[dict] = []
    for s in series:
        if not _SENATE_SERIES_RE.match(s["ticker"]):
            continue
        resp = _get_with_retry(
            client,
            f"{KALSHI_BASE}/events",
            params={"series_ticker": s["ticker"], "with_nested_markets": "true"},
        )
        time.sleep(REQUEST_DELAY_SECONDS)
        events.extend(resp.json().get("events") or [])
    return events


def kalshi_senate_match(senate_index: dict[str, list[dict]], state: str, party: str) -> tuple[str | None, float, str]:
    """(ticker, confidence, note) for a state Senate race."""
    evs = senate_index.get(state, [])
    if len(evs) != 1:
        found = ", ".join(e["event_ticker"] for e in evs) or "none"
        return None, 0.0, f"Kalshi 2026 Senate general event: expected exactly one, found {found}"
    ev = evs[0]
    hit = match_senate_party(ev.get("markets") or [], party)
    if hit is None:
        return None, 0.0, f"Kalshi event {ev['event_ticker']} has no unambiguous {party} market"
    label = next((m.get("yes_sub_title") for m in ev["markets"] if m["ticker"] == hit.ref), "")
    if party == "I":
        return hit.ref, 0.6, (
            f"Kalshi {hit.ref} is candidate-specific ({label} sworn in), SIG is any Independent"
        )
    return hit.ref, 0.9, f"Kalshi YES label {label!r} but rules resolve on party sworn in"


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
    parser.add_argument("--offices", nargs="*", help="redraft only these offices (e.g. Senate)")
    args = parser.parse_args()

    markets = load_markets(args.markets)
    try:
        existing = {r["platform_id"]: r for r in load_markets(args.output)}
    except FileNotFoundError:
        existing = {}

    def keep_existing(m: dict[str, str]) -> bool:
        old = existing.get(m["id"])
        if old is None:
            return False
        if old.get("verified") == "true":
            return True
        return bool(args.offices) and m["office"] not in args.offices

    with httpx.Client(timeout=15) as client:
        print("Fetching Kalshi Elections series index...", file=sys.stderr)
        series = fetch_kalshi_elections_series(client)
        gov_index, sen_index = build_kalshi_state_index(series)
        print(f"Kalshi: {len(gov_index)} governor states, {len(sen_index)} senate states indexed", file=sys.stderr)
        senate_index: dict[str, list[dict]] = {}
        if any(m["office"] == "Senate" and m["state"] != "US" and not keep_existing(m) for m in markets):
            senate_index = index_senate_events(fetch_kalshi_senate_events(client, series))
            print(f"Kalshi: 2026 Senate general events for {len(senate_index)} states", file=sys.stderr)

        # Cache per-race external market lookups so each race's several
        # SIG markets (R/D/I) share one Kalshi + one Polymarket call.
        kalshi_cache: dict[str, tuple[list[ExternalMarket], float, str]] = {}
        poly_cache: dict[str, tuple[list[ExternalMarket], float, str]] = {}

        rows: list[dict[str, str]] = []
        for i, m in enumerate(markets):
            if keep_existing(m):
                rows.append(existing[m["id"]])
                continue
            race_key = m["race_key"]
            office, state, district, party = m["office"], m["state"], m["district"], m["party"]

            if race_key not in poly_cache:
                poly_cache[race_key] = poly_match_for_race(client, office, state, district)
            poly_markets, poly_conf, poly_note = poly_cache[race_key]
            poly_hit = match_party(poly_markets, party)

            if office == "Senate" and state != "US":
                kalshi_ref, kalshi_conf, kalshi_note = kalshi_senate_match(senate_index, state, party)
            else:
                if race_key not in kalshi_cache:
                    kalshi_cache[race_key] = kalshi_match_for_race(client, office, state, gov_index, sen_index)
                kalshi_markets, kalshi_conf, kalshi_note = kalshi_cache[race_key]
                kalshi_hit = match_party(kalshi_markets, party)
                kalshi_ref = kalshi_hit.ref if kalshi_hit else None
                if kalshi_hit:
                    kalshi_note = ""
                else:
                    kalshi_conf = 0.0

            row = build_row(
                platform_id=m["id"],
                kalshi_ref=kalshi_ref,
                kalshi_confidence=kalshi_conf,
                kalshi_note=kalshi_note,
                poly_ref=poly_hit.ref if poly_hit else None,
                poly_confidence=poly_conf if poly_hit else 0.0,
                poly_note="" if poly_hit else poly_note,
            )
            rows.append(
                {
                    "platform_id": row.platform_id,
                    "kalshi_ticker": row.kalshi_ticker,
                    "poly_token_id": row.poly_token_id,
                    "polarity": row.polarity,
                    "rule_diff_notes": row.rule_diff_notes,
                    "confidence": str(row.confidence),
                    "verified": "false",
                    "tier": "",
                }
            )
            if (i + 1) % 20 == 0:
                print(f"  matched {i + 1}/{len(markets)} markets", file=sys.stderr)

    fieldnames = [
        "platform_id", "kalshi_ticker", "poly_token_id", "polarity",
        "rule_diff_notes", "confidence", "verified", "tier",
    ]  # fmt: skip
    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    kept = sum(1 for m in markets if keep_existing(m))
    print(f"Wrote {len(rows)} rows to {args.output} ({kept} copied unchanged). Nothing newly marked verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
