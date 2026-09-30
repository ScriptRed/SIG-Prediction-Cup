"""Launch report: every SIG market with a mapped Kalshi ticker, the Cup
book next to its Kalshi anchor, and flags for anything that should keep a
race off the launch list.

    python -m scripts.launch_report

Writes data/launch_report.csv (sorted by absolute SIG-vs-Kalshi gap) and
prints a summary: clean races, flagged races and why, and the top 10
longshot-overpricing markets (SIG best ask minus Kalshi price where Kalshi
is under 10%). Thresholds and the exclude list live in settings.yaml
`launch_report`.

Read-only against both venues: GET requests only, no orders, ever. SIG
reads always pass the Cup's tournamentId. Needs SIG_API_KEY in .env.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
from collections.abc import Callable
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

from predcup.launch_report import (
    CSV_COLUMNS,
    LaunchReportConfig,
    ReportRow,
    Summary,
    build_row,
    csv_record,
    flag_counts,
    sig_top_from_orderbook,
    sort_rows,
    summarize,
)
from predcup.mapping_review import read_csv
from predcup.venues.kalshi import KalshiEvent, KalshiNotFound, KalshiReadOnly
from scripts.fetch_cup_markets import CUP_SLUG
from scripts.show_mapping import MAP_PATH, MARKETS_PATH, SETTINGS_PATH, SigReadOnly


def _p(v: float | None) -> str:
    return "  -  " if v is None else f"{v:.3f}"


async def gather_rows(
    targets: list[tuple[dict[str, str], dict[str, str]]],
    sig: SigReadOnly,
    kalshi: KalshiReadOnly,
    slug: str,
    cfg: LaunchReportConfig,
    progress: Callable[[str], None],
) -> list[ReportRow]:
    tid = await sig.tournament_id(slug)
    events: dict[str, KalshiEvent | None] = {}
    rows = []
    for i, (cup, mp) in enumerate(targets, 1):
        top = sig_top_from_orderbook(await sig.orderbook(cup["exchange_id"], tid))
        try:
            k = await kalshi.get_market(mp["kalshi_ticker"])
        except KalshiNotFound:
            k = None
        ev = None
        if k is not None:
            if k.event_ticker not in events:
                try:
                    events[k.event_ticker] = await kalshi.get_event(k.event_ticker)
                except KalshiNotFound:
                    events[k.event_ticker] = None
            ev = events[k.event_ticker]
        rows.append(build_row(cup, mp, top, k, ev, cfg))
        if i % 25 == 0:
            progress(f"  fetched {i}/{len(targets)} markets")
    return rows


def write_csv(rows: list[ReportRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(csv_record(r) for r in rows)


def print_summary(summary: Summary, n_markets: int, out: Callable[[str], None]) -> None:
    n_races = len(summary.clean_races) + len(summary.flagged_races)
    out(f"{n_races} races with Kalshi tickers ({n_markets} SIG markets)")
    out(f"CLEAN: {len(summary.clean_races)} races")
    if summary.clean_races:
        out("  " + ", ".join(summary.clean_races))
    out(f"FLAGGED: {len(summary.flagged_races)} races")
    for kind, n in flag_counts(summary).items():
        out(f"  {n:>3} races: {kind}")
    out("")
    for race, lines in summary.flagged_races.items():
        out(f"  {race}: " + " | ".join(lines))
    out("")
    out("TOP LONGSHOT OVERPRICING (SIG best ask - Kalshi price, Kalshi < threshold)")
    out(f"  {'race':<12} {'pty':<3} {'SIG ask':>7} {'size':>6} {'Kalshi':>6} {'over':>6}  flags")
    for r in summary.top_longshots:
        size = "-" if r.sig_ask_size is None else f"{r.sig_ask_size:,.0f}"
        out(
            f"  {r.race_key:<12} {r.party:<3} {_p(r.sig_ask):>7} {size:>6} {_p(r.kalshi_mid_adj):>6}"
            f" {r.longshot_overpricing * 100:+5.1f}p  {'; '.join(r.flags) or '-'}"
        )


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    out: Callable[[str], None] = print,
    map_path: Path = MAP_PATH,
    markets_path: Path = MARKETS_PATH,
    settings_path: Path = SETTINGS_PATH,
    output_path: Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args(argv)

    with open(settings_path) as f:
        settings = yaml.safe_load(f)
    lr = settings["launch_report"]
    cfg = LaunchReportConfig(
        max_gap=lr["max_gap"],
        longshot_below=lr["longshot_below"],
        min_kalshi_volume=settings["mapping_review"]["min_kalshi_volume"],
        exclude_states=frozenset(lr["exclude_states"]),
        exclude_independent=lr["exclude_independent"],
    )
    output = output_path or Path(lr["output_path"])
    slug = settings["platform"].get("tournament_slug") or CUP_SLUG
    kcfg, scfg = settings["venues"]["kalshi"], settings["venues"]["sig"]

    map_by_id = {r["platform_id"]: r for r in read_csv(map_path)}
    targets = [
        (c, map_by_id[c["id"]])
        for c in read_csv(markets_path)
        if c["id"] in map_by_id and map_by_id[c["id"]].get("kalshi_ticker")
    ]

    load_dotenv()
    api_key = os.environ.get("SIG_API_KEY")
    if not api_key:
        out("SIG_API_KEY not set (check .env)")
        return 1

    async def run() -> list[ReportRow]:
        async with httpx.AsyncClient(timeout=15, transport=transport) as client:
            sig = SigReadOnly(client, settings["platform"]["base_url"], api_key, scfg.get("request_delay_seconds", 0))
            kalshi = KalshiReadOnly(client, kcfg["base_url"], kcfg["request_delay_seconds"])
            return await gather_rows(targets, sig, kalshi, slug, cfg, out)

    try:
        rows = sort_rows(asyncio.run(run()))
    except httpx.HTTPStatusError as e:
        out(f"read failed: {e.response.status_code} {e.request.url} {e.response.text[:300]}")
        return 1
    except httpx.HTTPError as e:
        out(f"read failed: {e!r}")
        return 1

    write_csv(rows, output)
    out(f"wrote {len(rows)} rows to {output}")
    out("")
    print_summary(summarize(rows), len(rows), out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
