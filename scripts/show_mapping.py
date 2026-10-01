"""Hand-verify config/market_map.csv one race at a time.

    python -m scripts.show_mapping <race_key>                 # e.g. MA-Senate
    python -m scripts.show_mapping --list
    python -m scripts.show_mapping <race_key> --mark-verified
    python -m scripts.show_mapping --summary <race_key> [<race_key> ...]

For every SIG market in the race: the SIG side (title, ids, party, Cup
best bid/ask, any rules text the API returns), the mapped Kalshi market
(titles, outcome names, rules, dates, bid/ask, volume), the polarity we
assumed stated in words, and warnings (primary or non-2026 contract, mids
> 10 points apart after polarity, wide Kalshi spread, low volume).

--mark-verified prints all of that, asks you to type `yes`, then sets
verified=true and tier=A on that race's rows only.

--summary prints one line per SIG market of the given races: race, party,
Kalshi ticker, candidate (Kalshi YES label), Kalshi party ID (ticker
suffix) and whether it matches the ID every other verified row uses for
that SIG party and polarity (ok / MISMATCH(X) / mixed / - for nothing to
compare), SIG bid/ask, Kalshi bid/ask, the polarity-adjusted mid gap in
points (SIG - Kalshi) and the same warnings as the full report.

Read-only against both venues: GET requests only, no orders, ever. SIG
reads always pass the Cup's tournamentId (docs/platform/SUMMARY.md).
Needs SIG_API_KEY in .env for the race report; --list works offline.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import os
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

from predcup.mapping_review import (
    ReviewThresholds,
    kalshi_mid_in_sig_terms,
    kalshi_party_id,
    mark_verified,
    party_id_check,
    party_id_consensus,
    polarity_statement,
    race_summary,
    races_in_order,
    read_csv,
    resolve_race_key,
    review_warnings,
)
from predcup.venues.kalshi import KalshiEvent, KalshiMarket, KalshiNotFound, KalshiReadOnly
from scripts.fetch_cup_markets import CUP_SLUG

MAP_PATH = Path("config/market_map.csv")
MARKETS_PATH = Path("data/cup_markets.csv")
SETTINGS_PATH = Path("config/settings.yaml")
PARTY_NAMES = {"R": "Republican", "D": "Democratic", "I": "Independent"}
MAX_RETRIES = 5

# Market schema fields that are dates/outcomes, not settlement text.
_NON_TEXT_KEYS = {"settlementDate", "settledWith", "settledOn"}


# --- SIG (read-only) ----------------------------------------------------------


class SigReadOnly:
    """GET-only reads of the Cup. Every call passes tournamentId."""

    def __init__(
        self, client: httpx.AsyncClient, base_url: str, api_key: str, request_delay_seconds: float = 0.0
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        # Rate limits are not in the spec (docs/platform/SUMMARY.md): pace.
        self._delay = request_delay_seconds

    async def _get(self, path: str, params: dict | None = None) -> dict:
        # Retry only 429/503 with exponential backoff + jitter (CLAUDE.md).
        for attempt in range(MAX_RETRIES):
            resp = await self._client.get(f"{self._base}{path}", headers=self._headers, params=params)
            if self._delay:
                await asyncio.sleep(self._delay)
            if resp.status_code in (429, 503):
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else 2**attempt + random.uniform(0, 1)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    async def tournament_id(self, slug: str) -> str:
        data = await self._get(f"/tournaments/{slug}")
        return data["id"]

    async def market(self, market_id: str, tournament_id: str) -> dict:
        return await self._get(f"/markets/{market_id}", {"tournamentId": tournament_id})

    async def price(self, exchange_id: str, tournament_id: str) -> dict:
        return await self._get(f"/exchanges/{exchange_id}/price", {"tournamentId": tournament_id})

    async def orderbook(self, exchange_id: str, tournament_id: str, depth: int = 1) -> dict:
        return await self._get(
            f"/exchanges/{exchange_id}/orderbook", {"tournamentId": tournament_id, "depth": depth}
        )


def sig_rules_text(raw_market: dict) -> list[tuple[str, str]]:
    """Any rules/description/settlement/resolution text in the market
    response. The spec's Market schema has none, so this is normally empty;
    it's checked anyway in case the live API returns more than the spec."""
    out = []
    for key, value in raw_market.items():
        if key in _NON_TEXT_KEYS or not isinstance(value, str) or not value.strip():
            continue
        if any(w in key.lower() for w in ("rule", "desc", "settle", "resolution")):
            out.append((key, value))
    return out


# --- Report -----------------------------------------------------------------


@dataclass
class MarketReport:
    cup_row: dict[str, str]
    map_row: dict[str, str] | None
    sig_market: dict | None = None
    sig_price: dict | None = None
    kalshi: KalshiMarket | None = None
    event: KalshiEvent | None = None
    warnings: list[str] = field(default_factory=list)


def _fmt(p: float | None) -> str:
    return "-" if p is None else f"{p:.3f}"


def render(r: MarketReport, out: Callable[[str], None]) -> None:
    c, m = r.cup_row, r.map_row or {}
    out("=" * 78)
    out(f"SIG  {c['title']}")
    out(f"     market id {c['id']}  exchange id {c['exchange_id']}  party {PARTY_NAMES.get(c['party'], c['party'])}")
    if r.sig_price is not None:
        out(
            f"     Cup book: bid {_fmt(r.sig_price.get('bestBid'))}  ask {_fmt(r.sig_price.get('bestAsk'))}"
            f"  last {_fmt(r.sig_price.get('latestPrice'))}"
        )
    if r.sig_market is not None:
        out(f"     status {r.sig_market.get('status')}  settlementDate {r.sig_market.get('settlementDate')}")
        texts = sig_rules_text(r.sig_market)
        if texts:
            for key, value in texts:
                out(f"     {key}: {value}")
        else:
            out("     rules: NONE - the SIG markets API returned no rules/description/settlement text")

    ticker = m.get("kalshi_ticker", "")
    out("")
    if not ticker:
        out("KALSHI  (no ticker mapped)")
    elif r.kalshi is None:
        out(f"KALSHI  {ticker}: could not be fetched")
    else:
        k = r.kalshi
        out(f"KALSHI  {k.ticker}  (event {k.event_ticker}, status {k.status})")
        if r.event:
            out(f"     event: {r.event.title}" + (f" - {r.event.sub_title}" if r.event.sub_title else ""))
        out(f"     market: {k.title}")
        if k.subtitle:
            out(f"     subtitle: {k.subtitle}")
        out(f"     YES outcome: {k.yes_sub_title or '-'}   NO outcome: {k.no_sub_title or '-'}")
        if k.custom_strike:
            out(f"     custom_strike: {k.custom_strike}")
        out(f"     close {k.close_time}  expected expiration {k.expected_expiration_time}")
        out(
            f"     bid {_fmt(k.yes_bid)}  ask {_fmt(k.yes_ask)}  last {_fmt(k.last_price)}"
            f"  volume {k.volume:,.0f} (24h {k.volume_24h:,.0f})"
        )
        out(f"     rules: {k.rules_primary or '-'}")
        if k.rules_secondary:
            out(f"     rules (secondary): {k.rules_secondary}")
        if r.event and r.event.settlement_sources:
            out(f"     settlement sources: {'; '.join(r.event.settlement_sources)}")

    out("")
    if r.map_row is None:
        out("MAPPING  no row in config/market_map.csv for this market")
    else:
        out(f"MAPPING  {polarity_statement(c['title'], m.get('polarity', ''), r.kalshi, ticker)}")
        out(f"     rule_diff_notes: {m.get('rule_diff_notes') or '-'}")
        out(
            f"     confidence {m.get('confidence')}  verified {m.get('verified')}  tier {m.get('tier') or '-'}"
            f"  fusion_risk {m.get('fusion_risk') or '-'}"
        )
    if r.warnings:
        out("")
        for w in r.warnings:
            out(f"  !!! WARNING: {w}")


async def build_reports(
    cup_rows: list[dict[str, str]],
    map_by_id: dict[str, dict[str, str]],
    sig: SigReadOnly,
    kalshi: KalshiReadOnly,
    slug: str,
    th: ReviewThresholds,
) -> list[MarketReport]:
    tid = await sig.tournament_id(slug)
    events: dict[str, KalshiEvent | None] = {}
    reports = []
    for c in cup_rows:
        r = MarketReport(cup_row=c, map_row=map_by_id.get(c["id"]))
        r.sig_market = await sig.market(c["id"], tid)
        r.sig_price = await sig.price(c["exchange_id"], tid)
        ticker = (r.map_row or {}).get("kalshi_ticker", "")
        if ticker:
            try:
                r.kalshi = await kalshi.get_market(ticker)
            except KalshiNotFound:
                r.kalshi = None
        if r.kalshi is not None:
            et = r.kalshi.event_ticker
            if et not in events:
                try:
                    events[et] = await kalshi.get_event(et)
                except KalshiNotFound:
                    events[et] = None
            r.event = events[et]
        if r.map_row is None:
            r.warnings = ["no row in config/market_map.csv"]
        else:
            r.warnings = review_warnings(
                sig_state=c["state"],
                sig_bid=r.sig_price.get("bestBid"),
                sig_ask=r.sig_price.get("bestAsk"),
                polarity=r.map_row.get("polarity", ""),
                ticker=ticker,
                kalshi=r.kalshi,
                event=r.event,
                th=th,
            )
        reports.append(r)
    return reports


# --- --summary ----------------------------------------------------------------


def summary_header() -> str:
    return (
        f"{'id':<5} {'race':<16} {'pty':<3} {'kalshi ticker':<20} {'candidate':<22} {'k-id':<6} "
        f"{'k-id check':<12} SIG bid/ask, Kalshi bid/ask, gap (SIG - Kalshi mid, points), warnings"
    )


def summary_line(r: MarketReport, map_rows: list[dict[str, str]], party_of: dict[str, str]) -> str:
    c, m = r.cup_row, r.map_row or {}
    ticker = m.get("kalshi_ticker", "")
    polarity = m.get("polarity", "")
    kid = kalshi_party_id(ticker)
    consensus = party_id_consensus(map_rows, party_of, party=c["party"], polarity=polarity, exclude_id=c["id"])
    check = party_id_check(kid, consensus)
    warnings = list(r.warnings)
    if check.startswith(("MISMATCH", "mixed")):
        others = "/".join(sorted(consensus))
        warnings.insert(0, f"Kalshi party ID {kid}, other verified {c['party']} rows use {others}")

    sp = r.sig_price or {}
    s_bid, s_ask = sp.get("bestBid"), sp.get("bestAsk")
    k = r.kalshi
    gap = "-"
    k_mid = kalshi_mid_in_sig_terms(k, polarity) if k else None
    if k_mid is not None and s_bid is not None and s_ask is not None:
        gap = f"{((s_bid + s_ask) / 2 - k_mid) * 100:+.1f}"
    candidate = (k.yes_sub_title if k else "")[:22] or "-"
    return (
        f"{c['id']:<5} {c['race_key']:<16} {c['party']:<3} {ticker or '-':<20} {candidate:<22} {kid or '-':<6} "
        f"{check:<12} SIG {_fmt(s_bid)}/{_fmt(s_ask)}  K {_fmt(k.yes_bid if k else None)}/"
        f"{_fmt(k.yes_ask if k else None)}  gap {gap:<5}  {'; '.join(warnings) or '-'}"
    )


# --- CLI --------------------------------------------------------------------


def _load_settings(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def cmd_list(races: dict[str, list[dict[str, str]]], map_by_id: dict[str, dict[str, str]], out) -> int:
    out(f"{'race_key':<20} {'mkts':>4} {'tier':<6} {'verified':<9} {'confidence':<11} kalshi")
    for key, cup_rows in races.items():
        rows = [map_by_id[c["id"]] for c in cup_rows if c["id"] in map_by_id]
        tier, ver, conf, with_kalshi = race_summary(rows)
        missing = len(cup_rows) - len(rows)
        note = f"  ({missing} not in map)" if missing else ""
        out(f"{key:<20} {len(cup_rows):>4} {tier:<6} {ver:<9} {conf:<11} {with_kalshi}/{len(cup_rows)}{note}")
    return 0


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    input_fn: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    map_path: Path = MAP_PATH,
    markets_path: Path = MARKETS_PATH,
    settings_path: Path = SETTINGS_PATH,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("race_key", nargs="*")
    parser.add_argument("--list", action="store_true", help="list races with tier, verified, confidence")
    parser.add_argument("--mark-verified", action="store_true", help="after review, set verified=true tier=A")
    parser.add_argument("--summary", action="store_true", help="one line per SIG market of the given races")
    args = parser.parse_args(argv)

    cup_markets = read_csv(markets_path)
    races = races_in_order(cup_markets)
    map_rows = read_csv(map_path)
    map_by_id = {r["platform_id"]: r for r in map_rows}

    if args.list:
        return cmd_list(races, map_by_id, out)
    if not args.race_key:
        parser.error("give a race_key, or --list")
    if args.summary and args.mark_verified:
        parser.error("--summary is read-only; use --mark-verified on one race without --summary")
    if not args.summary and len(args.race_key) > 1:
        parser.error("give one race_key (several only with --summary)")

    keys = []
    for requested in args.race_key:
        key = resolve_race_key(requested, list(races))
        if key is None:
            close = difflib.get_close_matches(requested, list(races), n=5)
            out(f"unknown race_key {requested!r}." + (f" Did you mean: {', '.join(close)}?" if close else ""))
            return 2
        keys.append(key)
    key = keys[0]
    cup_rows = [c for k in dict.fromkeys(keys) for c in races[k]]

    settings = _load_settings(settings_path)
    rv = settings["mapping_review"]
    th = ReviewThresholds(
        max_mid_diff=rv["max_mid_diff"],
        max_kalshi_spread=rv["max_kalshi_spread"],
        min_kalshi_volume=rv["min_kalshi_volume"],
    )
    kcfg = settings["venues"]["kalshi"]
    slug = settings["platform"].get("tournament_slug") or CUP_SLUG

    load_dotenv()
    api_key = os.environ.get("SIG_API_KEY")
    if not api_key:
        out("SIG_API_KEY not set (check .env)")
        return 1

    async def run() -> list[MarketReport]:
        async with httpx.AsyncClient(timeout=15, transport=transport) as client:
            sig = SigReadOnly(client, settings["platform"]["base_url"], api_key)
            kalshi = KalshiReadOnly(client, kcfg["base_url"], kcfg["request_delay_seconds"])
            return await build_reports(cup_rows, map_by_id, sig, kalshi, slug, th)

    try:
        reports = asyncio.run(run())
    except httpx.HTTPStatusError as e:
        out(f"read failed: {e.response.status_code} {e.request.url} {e.response.text[:300]}")
        return 1
    except httpx.HTTPError as e:
        out(f"read failed: {e!r}")
        return 1

    if args.summary:
        party_of = {c["id"]: c["party"] for c in cup_markets}
        out(summary_header())
        for r in reports:
            out(summary_line(r, map_rows, party_of))
        return 0

    out(f"Race {key}: {len(cup_rows)} SIG market(s)")
    for r in reports:
        render(r, out)
    n_warn = sum(len(r.warnings) for r in reports)
    out("=" * 78)
    out(f"{n_warn} warning(s) in {key}." if n_warn else f"No warnings in {key}.")

    if not args.mark_verified:
        return 0

    unmapped = [r.cup_row["id"] for r in reports if r.map_row is None or not r.map_row.get("kalshi_ticker")]
    if unmapped:
        out(f"Refusing to mark verified: market(s) {', '.join(unmapped)} have no Kalshi ticker mapped.")
        return 1
    ids = {r.cup_row["id"] for r in reports}
    answer = input_fn(
        f"Mark {len(ids)} row(s) of {key} ({', '.join(sorted(ids))}) verified=true, tier=A"
        f" despite {n_warn} warning(s)? Type yes: "
        if n_warn
        else f"Mark {len(ids)} row(s) of {key} ({', '.join(sorted(ids))}) verified=true, tier=A? Type yes: "
    )
    if answer.strip() != "yes":
        out("Not marked. market_map.csv unchanged.")
        return 1
    changed = mark_verified(map_path, ids)
    out(f"Marked {changed} row(s) verified=true, tier=A in {map_path}. No other rows changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
