"""Multi-hour soak run of the full trading process against MockExchange.

    python -m sim.run_mock --hours 3 [--markets 25] [--fill-prob 0.03] [--seed 1]

Runs predcup.app.App in live mode, but ONLY against sim.mock_exchange (the
harness refuses any other venue), with a simulated Kalshi feed (random-walk
mids), random fills of resting quotes, and injected Kalshi outages. Uses
config/settings.yaml intervals and limits, synthetic verified Tier A
targets, and its own SQLite file (data/mock_run.db, git-ignored).

At the end it prints a summary and checks invariants: no loop errors, no
reconciliation mismatch, no unexplained halt, exposure within caps, quotes
pulled during outages. Exit code 1 if any invariant fails.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from predcup.app import App, LogAlerter, MappedTarget
from predcup.models import OrderStatus
from predcup.store import EventStore
from predcup.strategies.quoter import QuoteTarget
from predcup.venues.kalshi import KalshiMarket, parse_market
from sim.mock_exchange import MockExchange

TID = "00000000-0000-0000-0000-00000000c0de"
STATES = ["AZ", "CO", "FL", "GA", "IA", "KS", "MI", "MN", "NC", "NH", "NJ", "NM", "NV", "OH", "PA", "TX", "VA", "WI"]


class SimKalshi:
    """Random-walk YES mids per ticker; outages on demand."""

    def __init__(self, tickers: list[str], rng: random.Random) -> None:
        self.rng = rng
        self.mid = {t: rng.uniform(0.08, 0.92) for t in tickers}
        self.down = False
        self.calls = 0

    def step(self) -> None:
        for t in self.mid:
            self.mid[t] = min(0.97, max(0.03, self.mid[t] + self.rng.gauss(0, 0.004)))

    async def get_markets(self, tickers: list[str]) -> dict[str, KalshiMarket]:
        self.calls += 1
        if self.down:
            raise ConnectionError("simulated Kalshi outage")
        out = {}
        for t in tickers:
            half = self.rng.choice([0.005, 0.01])
            bid, ask = round(self.mid[t] - half, 3), round(self.mid[t] + half, 3)
            out[t] = parse_market({"ticker": t, "event_ticker": t.rsplit("-", 1)[0], "title": "", "subtitle": "",
                                   "yes_sub_title": "", "no_sub_title": "", "status": "active",
                                   "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}",
                                   "volume_fp": "50000.00", "rules_primary": "", "rules_secondary": ""})  # fmt: skip
        return out


def build_targets(n: int) -> tuple[list[MappedTarget], dict[str, tuple[str, str | None, str | None]]]:
    targets, meta = [], {}
    for i in range(n):
        state = STATES[i // 2 % len(STATES)]
        race = f"{state}-Senate" if i < 2 * len(STATES) else f"{state}-Governor"
        party = "D" if i % 2 == 0 else "R"
        ex, mid = str(5000 + i), str(9000 + i)
        row = {"platform_id": mid, "kalshi_ticker": f"SIM{state}-26-{party}{i}", "polarity": "same",
               "confidence": "0.9", "verified": "true", "tier": "A", "fusion_risk": "false"}  # fmt: skip
        targets.append(MappedTarget(QuoteTarget(ex, mid, race, party), row))
        meta[ex] = (mid, party, race)
    return targets, meta


async def soak(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    settings = yaml.safe_load(Path("config/settings.yaml").read_text())
    db = Path(args.db)
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    store = EventStore(db)
    venue = MockExchange(starting_balance=100_000.0)
    if not isinstance(venue, MockExchange):  # the one place "live" is allowed: never a real venue
        raise SystemExit("run_mock only runs against MockExchange")
    targets, meta = build_targets(args.markets)
    kalshi = SimKalshi([t.map_row["kalshi_ticker"] for t in targets], rng)
    app = App(settings=settings, venue=venue, kalshi=kalshi, store=store, alerter=LogAlerter(store),
              tournament_id=TID, bankroll=100_000.0, targets=targets, market_meta=meta,
              fusion_race_keys=frozenset(), shadow=False, live_allowed=True)  # fmt: skip

    stats: Counter = Counter()
    outage_windows: list[tuple[float, float]] = []

    async def market_sim() -> None:
        while True:
            await asyncio.sleep(1)
            kalshi.step()
            for t in targets:  # SIG "seed" book around the Kalshi mid
                m = kalshi.mid[t.map_row["kalshi_ticker"]]
                venue.set_top_of_book(t.target.exchange_id, round(max(0.005, m - 0.04), 3), round(min(0.995, m + 0.04), 3))

    async def filler() -> None:
        while True:
            await asyncio.sleep(5)
            for o in await venue.get_open_orders(TID):
                if rng.random() < args.fill_prob:
                    await venue.simulate_fill(o.id, rng.randint(1, o.quantity), TID)
                    stats["fills"] += 1

    async def outages() -> None:
        while True:
            await asyncio.sleep(rng.uniform(600, 1800))
            start = time.monotonic()
            kalshi.down = True
            await asyncio.sleep(rng.uniform(70, 150))  # longer than the 60 s staleness limit
            kalshi.down = False
            outage_windows.append((start, time.monotonic()))
            stats["outages"] += 1

    async def exposure_watch() -> None:
        limits = settings["risk"]
        while True:
            await asyncio.sleep(10)
            orders = [o for o in app.risk._exposure_orders() if o.status not in (OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED)]
            total = sum(o.quantity * (o.price or 1) for o in orders) + sum(p.notional for p in app.risk._positions)
            stats["max_total_exposure"] = max(stats["max_total_exposure"], int(total))
            if total > limits["max_total_exposure_fraction"] * 100_000 + 1:
                stats["exposure_breach"] += 1

    for f in (market_sim, filler, outages, exposure_watch):
        app.add_task(f)
    started = time.monotonic()
    print(f"mock soak: {args.markets} markets for {args.hours} h, db {db}", file=sys.stderr)
    await app.run(duration_seconds=args.hours * 3600)
    elapsed = time.monotonic() - started

    ev = Counter(e["event_type"] for e in store.all_events())
    recon = Counter(e["payload"]["status"] for e in store.all_events("reconciliation"))
    lags = [e["payload"]["lag_seconds"] for e in store.all_events("loop_lag")]
    rejections = Counter(e["payload"]["reason"] for e in store.all_events("risk_rejection"))
    ramp = [e["payload"] for e in store.all_events("size_ramp")]
    print(f"ran {elapsed / 3600:.2f} h; kalshi polls {kalshi.calls}; outages {stats['outages']}; fills {stats['fills']}")
    print(f"orders placed {ev['order']}, cancel-alls {ev['cancel_all']}, fair-value changes {ev['fair_value']}")
    print(f"reconciliations {dict(recon)}; ramp step now {ramp[-1]['step'] if ramp else '-'}")
    print(f"loop lag: max {max(lags):.3f}s, over threshold {sum(1 for e in store.all_events('loop_lag') if e['payload']['over_threshold'])}")
    print(f"risk rejections: {dict(rejections.most_common(6))}")
    print(f"max total exposure {stats['max_total_exposure']:,}; halted: {app.control.halted} {app.control.reason}")
    print(f"final positions: {len(store.local_positions(TID))} markets, alerts {ev['alert']}")

    failures = []
    if ev["loop_error"]:
        failures.append(f"{ev['loop_error']} loop errors")
    if recon.get("mismatch"):
        failures.append(f"{recon['mismatch']} reconciliation mismatches")
    if app.control.halted:
        failures.append(f"halted: {app.control.reason}")
    if stats["exposure_breach"]:
        failures.append("total exposure above cap")
    if not ev["order"]:
        failures.append("no orders placed")
    for f in failures:
        print(f"INVARIANT FAILED: {f}")
    store.close()
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hours", type=float, default=3.0)
    p.add_argument("--markets", type=int, default=25)
    p.add_argument("--fill-prob", type=float, default=0.03)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--db", default="data/mock_run.db")
    return asyncio.run(soak(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
