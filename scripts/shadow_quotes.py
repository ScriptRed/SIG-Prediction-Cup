"""Shadow-mode viewer for go-live gate (b): the latest intended quote per
market (bid/ask and size, from events_log `shadow_quote`) next to SIG's
current best bid/ask and the Kalshi fair value it was built from.

    python -m scripts.shadow_quotes [--minutes 60] [--offline] [--db data/predcup.db]

Read-only: opens the SQLite file read-only (safe while the bot runs) and
makes one GET /exchanges/prices read (Cup tournamentId) for the current
SIG book; --offline skips it. Flags a quote that would cross SIG's book
(post_only backs those off in live mode) and fair values that are not
available. Places nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

from predcup.venues.sig import SigVenue

SETTINGS_PATH = Path("config/settings.yaml")
MARKETS_PATH = Path("data/cup_markets.csv")


@dataclass
class QuoteView:
    exchange_id: str
    bid: float | None = None
    bid_size: int | None = None
    ask: float | None = None
    ask_size: int | None = None
    at: datetime | None = None  # newest side's time
    fair_value: float | None = None  # from the newest fair_value event, else the quote's own
    fv_reason: str = ""


def read_events(db: str | Path, since: datetime) -> list[tuple[datetime, str, dict]]:
    conn = sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT ts, event_type, payload FROM events_log WHERE event_type IN ('shadow_quote', 'fair_value') "
            "AND ts >= ? ORDER BY id",
            (since.astimezone(timezone.utc).isoformat(),),
        ).fetchall()
    finally:
        conn.close()
    return [(datetime.fromisoformat(ts), et, json.loads(p)) for ts, et, p in rows]


def latest_quotes(events: list[tuple[datetime, str, dict]]) -> dict[str, QuoteView]:
    """Latest bid and ask per exchange (events are oldest first), plus the
    latest fair value logged for that exchange."""
    views: dict[str, QuoteView] = {}
    fvs: dict[str, dict] = {}
    for ts, et, p in events:
        ex = str(p.get("exchange_id"))
        if et == "fair_value":
            fvs[ex] = p
            continue
        v = views.setdefault(ex, QuoteView(ex))
        if p.get("action") == "buy":
            v.bid, v.bid_size = p.get("price"), p.get("quantity")
        else:
            v.ask, v.ask_size = p.get("price"), p.get("quantity")
        v.at = ts
        v.fair_value = p.get("fair_value")
    for ex, v in views.items():
        if ex in fvs:
            v.fair_value = fvs[ex].get("value")
            v.fv_reason = fvs[ex].get("reason") or ""
    return views


def flags(v: QuoteView, sig_bid: float | None, sig_ask: float | None) -> list[str]:
    out = []
    if (v.bid is not None and sig_ask is not None and v.bid >= sig_ask - 1e-9) or (
        v.ask is not None and sig_bid is not None and v.ask <= sig_bid + 1e-9
    ):
        out.append("CROSSES SIG")
    return out


def _p(x: float | None) -> str:
    return "  -  " if x is None else f"{x:.3f}"


def _side(price: float | None, size: int | None) -> str:
    return f"{'  -  ':<10}" if price is None else f"{price:.3f} x{size:<4}"


async def _sig_books(settings: dict, ids: list[str], transport) -> dict[str, tuple[float | None, float | None]]:
    async with httpx.AsyncClient(timeout=15, transport=transport) as client:
        venue = SigVenue(client, base_url=settings["platform"]["base_url"], api_key=os.environ["SIG_API_KEY"],
                         tournament_slug=settings["platform"]["tournament_slug"],
                         on_rate_limited=lambda endpoint, retry_after: None)  # fmt: skip
        tid = await venue.tournament_id()
        return await venue.get_top_of_books(ids, tid)


def main(
    argv: list[str] | None = None,
    *,
    out: Callable[[str], None] = print,
    settings_path: Path = SETTINGS_PATH,
    markets_path: Path = MARKETS_PATH,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    settings = yaml.safe_load(settings_path.read_text())
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=settings["storage"]["db_path"])
    p.add_argument("--minutes", type=float, default=60, help="look back this far for quotes")
    p.add_argument("--offline", action="store_true", help="don't read SIG's current book")
    a = p.parse_args(argv)
    if not Path(a.db).exists():
        out(f"no database at {a.db}")
        return 1

    t = now()
    views = latest_quotes(read_events(a.db, t - timedelta(minutes=a.minutes)))
    if not views:
        out(f"no shadow quotes in the last {a.minutes:g} minutes ({a.db})")
        return 0
    labels: dict[str, str] = {}
    if markets_path.exists():
        with open(markets_path, newline="") as f:
            labels = {r["exchange_id"]: f"{r['race_key']} {r['party']}" for r in csv.DictReader(f)}

    books: dict[str, tuple[float | None, float | None]] = {}
    if not a.offline:
        load_dotenv(".env")
        try:
            books = asyncio.run(_sig_books(settings, sorted(views), transport))
        except httpx.HTTPError as e:
            out(f"SIG read failed ({e!r}); showing quotes without the live book")

    out(f"{'market':<16} {'ex':<6} {'our bid':<10} {'our ask':<10} {'age':>5}  {'SIG bid/ask':<15} {'Kalshi FV':<14} flags")
    rows = sorted(views.values(), key=lambda v: labels.get(v.exchange_id, v.exchange_id))
    for v in rows:
        sig_bid, sig_ask = books.get(v.exchange_id, (None, None))
        sig = "SIG n/a" if a.offline or v.exchange_id not in books else f"SIG {_p(sig_bid)}/{_p(sig_ask)}"
        fv = f"FV {v.fair_value:.3f}" if v.fair_value is not None else f"FV n/a ({v.fv_reason or 'none'})"
        age = f"{(t - v.at).total_seconds():.0f}s" if v.at else "-"
        out(f"{labels.get(v.exchange_id, v.exchange_id):<16} {v.exchange_id:<6} {_side(v.bid, v.bid_size)} "
            f"{_side(v.ask, v.ask_size)} {age:>5}  {sig:<15} {fv:<14} {' '.join(flags(v, sig_bid, sig_ask)) or '-'}")  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
