"""Launch report: every SIG market with a mapped Kalshi ticker, the Cup
book next to its Kalshi anchor, and flags for anything that should keep a
race off the launch list.

    python -m scripts.launch_report

Writes data/launch_report.csv (sorted by absolute SIG-vs-Kalshi gap, with
the tradeable edges SIG bid - Kalshi ask and Kalshi bid - SIG ask) and
data/launch_report_unmapped.csv (SIG markets with no Kalshi ticker), and
prints: clean races, flagged races and why, a "trade by hand at the open"
list (SIG quotes crossing Kalshi's by 2+ points), the top 10
longshot-overpricing markets, and the unmapped races' Cup books.
Thresholds and the exclude list live in settings.yaml `launch_report`.

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
    HandTrade,
    ReportRow,
    Summary,
    UnmappedRow,
    build_row,
    hand_trades,
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
    unmapped: list[dict[str, str]],
) -> tuple[list[ReportRow], list[UnmappedRow]]:
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
    unmapped_rows = []
    for cup in unmapped:
        top = sig_top_from_orderbook(await sig.orderbook(cup["exchange_id"], tid))
        unmapped_rows.append(UnmappedRow.from_book(cup, top))
    return rows, unmapped_rows


def write_unmapped_csv(rows: list[UnmappedRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["race_key", "party", "sig_market_id", "sig_title", "bid", "bid_size", "ask", "ask_size", "mid", "spread"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols)
        for r in rows:
            w.writerow(["" if (v := getattr(r, c)) is None else (f"{v:.4f}" if isinstance(v, float) else v) for c in cols])


def print_hand_trades(trades: list[HandTrade], min_edge: float, out: Callable[[str], None]) -> None:
    out(f"TRADE BY HAND AT THE OPEN (SIG quote crosses Kalshi's by >= {min_edge * 100:.0f} pts; check flags first)")
    if not trades:
        out("  none")
        return
    out(f"  {'race':<12} {'pty':<3} {'action':<20} {'price':>6} {'size':>6} {'Kalshi':>6} {'edge':>6}  flags")
    for t in trades:
        size = "-" if t.size is None else f"{t.size:,.0f}"
        out(
            f"  {t.row.race_key:<12} {t.row.party:<3} {t.direction:<20} {t.price:>6.3f} {size:>6}"
            f" {t.kalshi_price:>6.3f} {t.edge * 100:+5.1f}p  {'; '.join(t.row.flags) or '-'}"
        )


def print_unmapped(rows: list[UnmappedRow], out: Callable[[str], None]) -> None:
    races = sorted({r.race_key for r in rows})
    out(f"NO KALSHI MAPPING: {len(races)} races, {len(rows)} SIG markets (Cup book only)")
    out(f"  {'race':<14} {'pty':<3} {'bid':>6} {'size':>6} {'ask':>6} {'size':>6} {'mid':>6}")
    for r in sorted(rows, key=lambda r: (r.race_key, r.party)):
        bs = "-" if r.bid_size is None else f"{r.bid_size:,.0f}"
        as_ = "-" if r.ask_size is None else f"{r.ask_size:,.0f}"
        out(f"  {r.race_key:<14} {r.party:<3} {_p(r.bid):>6} {bs:>6} {_p(r.ask):>6} {as_:>6} {_p(r.mid):>6}")


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
    unmapped_output = output.with_name(Path(lr["unmapped_output_path"]).name) if output_path else Path(lr["unmapped_output_path"])
    slug = settings["platform"].get("tournament_slug") or CUP_SLUG
    kcfg, scfg = settings["venues"]["kalshi"], settings["venues"]["sig"]

    map_by_id = {r["platform_id"]: r for r in read_csv(map_path)}
    cup_rows = read_csv(markets_path)
    targets = [
        (c, map_by_id[c["id"]])
        for c in cup_rows
        if c["id"] in map_by_id and map_by_id[c["id"]].get("kalshi_ticker")
    ]
    mapped_ids = {c["id"] for c, _ in targets}
    unmapped = [c for c in cup_rows if c["id"] not in mapped_ids]

    load_dotenv()
    api_key = os.environ.get("SIG_API_KEY")
    if not api_key:
        out("SIG_API_KEY not set (check .env)")
        return 1

    async def run() -> tuple[list[ReportRow], list[UnmappedRow]]:
        async with httpx.AsyncClient(timeout=15, transport=transport) as client:
            sig = SigReadOnly(client, settings["platform"]["base_url"], api_key, scfg.get("request_delay_seconds", 0))
            kalshi = KalshiReadOnly(client, kcfg["base_url"], kcfg["request_delay_seconds"])
            return await gather_rows(targets, sig, kalshi, slug, cfg, out, unmapped)

    try:
        rows, unmapped_rows = asyncio.run(run())
        rows = sort_rows(rows)
    except httpx.HTTPStatusError as e:
        out(f"read failed: {e.response.status_code} {e.request.url} {e.response.text[:300]}")
        return 1
    except httpx.HTTPError as e:
        out(f"read failed: {e!r}")
        return 1

    write_csv(rows, output)
    write_unmapped_csv(unmapped_rows, unmapped_output)
    out(f"wrote {len(rows)} rows to {output}, {len(unmapped_rows)} unmapped to {unmapped_output}")
    out("")
    print_summary(summarize(rows), len(rows), out)
    out("")
    min_edge = lr["hand_trade_min_edge"]
    print_hand_trades(hand_trades(rows, min_edge), min_edge, out)
    out("")
    print_unmapped(unmapped_rows, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
