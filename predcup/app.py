"""The trading process, wired: venue, Kalshi feed, fair value, quoter,
order router, risk manager, size ramp, fusion set, loop-lag metric and the
halt/kill path. main.py only reads config and secrets and calls this.

Shadow mode is the only mode this build runs: LIVE_ENABLED is False and
App refuses shadow=False unless live_allowed is passed explicitly (tests
only). Going live is a code change the user approves (PLAN launch status).

Hooks for the safety branch (KILL file watcher, Telegram):
- App.request_kill(reason): halt quoting, then the halt handler runs the
  kill switch (cancel all + confirm) in live mode.
- App.reset_ramp(reason): Telegram /resetramp.
- `alerter` is injected: Telegram replaces the default log alerter.
- App.add_task(coro_factory): extra long-running tasks (watchers) run
  alongside the loops and are cancelled with them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from predcup.control import TradingControl
from predcup.fairvalue import FairValueTracker, KalshiQuote, kalshi_fair_value, load_fair_value_config
from predcup.looplag import LoopLagMonitor, load_loop_lag_config
from predcup.orders import OrderRouter
from predcup.reconcile import Reconciler
from predcup.risk import Alerter, RiskManager, SizeRamp, load_risk_limits, load_size_ramp_config
from predcup.store import EventStore
from predcup.strategies.quoter import QuoteTarget, Quoter, load_quoter_config
from predcup.venues.kalshi import KalshiMarket

LIVE_ENABLED = False  # flip only when the user says go (docs/PLAN.md launch status)


@dataclass(frozen=True)
class MappedTarget:
    target: QuoteTarget
    map_row: dict[str, str]


def load_targets(cup_rows: list[dict[str, str]], map_rows: list[dict[str, str]]) -> list[MappedTarget]:
    """Markets the bot may quote: market_map rows that are verified, Tier A
    and have a Kalshi ticker (fairvalue.py re-checks all of this)."""
    by_id = {r["platform_id"]: r for r in map_rows}
    out = []
    for c in cup_rows:
        row = by_id.get(c["id"])
        if not row or row.get("verified", "").lower() != "true" or row.get("tier", "").upper() != "A":
            continue
        if not row.get("kalshi_ticker"):
            continue
        out.append(MappedTarget(QuoteTarget(c["exchange_id"], c["id"], c["race_key"], c["party"]), row))
    return out


class LogAlerter:
    """Default alerter until Telegram is merged: events_log + stderr."""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    def send(self, message: str) -> None:
        import sys

        self._store.log("alert", {"message": message})
        print(f"ALERT: {message}", file=sys.stderr)


class App:
    def __init__(
        self,
        *,
        settings: dict,
        venue,
        kalshi,  # KalshiReadOnly-like: async get_markets(tickers) -> {ticker: KalshiMarket}
        store: EventStore,
        alerter: Alerter,
        tournament_id: str,
        bankroll: float,
        targets: list[MappedTarget],
        market_meta: dict[str, tuple[str, str | None, str | None]],  # exchange_id -> (market_id, party, race_key)
        fusion_race_keys: frozenset[str],
        shadow: bool = True,
        live_allowed: bool = LIVE_ENABLED,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        if not shadow and not live_allowed:
            raise RuntimeError("live mode is disabled in this build; run in shadow mode")
        self.settings = settings
        self.venue = venue
        self.kalshi = kalshi
        self.store = store
        self.alerter = alerter
        self.tid = tournament_id
        self.targets = targets
        self.shadow = shadow
        self.clock = clock
        self.mono = mono
        self.control = TradingControl()
        self.ramp = SizeRamp(load_size_ramp_config(settings), store, alerter)
        self.risk = RiskManager(
            limits=load_risk_limits(settings), bankroll=bankroll, event_store=store, venue=venue,
            tournament_id=tournament_id, alerter=alerter, size_ramp=self.ramp, fusion_race_keys=fusion_race_keys,
        )  # fmt: skip
        self.router = OrderRouter(venue=venue, risk=self.risk, store=store, tournament_id=tournament_id,
                                  shadow=shadow, alerter=alerter, control=self.control)  # fmt: skip
        self.fv_cfg = load_fair_value_config(settings)
        self.fair_values = FairValueTracker(store)
        self.quoter_cfg = load_quoter_config(settings)
        self.quoter = Quoter(
            router=self.router, fair_values=self.fair_values,
            positions=lambda: store.local_positions(tournament_id),
            books=lambda ids: venue.get_top_of_books(ids, tournament_id),
            cfg=self.quoter_cfg, tournament_id=tournament_id, control=self.control,
        )  # fmt: skip
        self.reconciler = Reconciler(
            venue=venue, store=store, risk=self.risk, router=self.router, control=self.control, alerter=alerter,
            tournament_id=tournament_id, market_meta=market_meta, shadow=shadow,
            max_read_failures=int(settings["risk"]["reconciliation_max_read_failures"]),
        )  # fmt: skip
        self.looplag = LoopLagMonitor(load_loop_lag_config(settings), store, alerter, clock=mono)
        self._quotes: dict[str, KalshiQuote] = {}
        self._halt_handled = False
        self._extra_tasks: list[Callable[[], Awaitable[None]]] = []
        store.log("app_start", {"shadow": shadow, "targets": [t.target.exchange_id for t in targets],
                                "tournament_id": tournament_id})  # fmt: skip

    # --- hooks for the safety branch ------------------------------------------------

    def request_kill(self, reason: str) -> None:
        self.control.halt(reason)

    def reset_ramp(self, reason: str) -> None:
        self.ramp.reset(reason)

    def add_task(self, factory: Callable[[], Awaitable[None]]) -> None:
        self._extra_tasks.append(factory)

    # --- one step of each loop (tests drive these directly) ----------------------------

    async def poll_kalshi_once(self) -> None:
        tickers = sorted({t.map_row["kalshi_ticker"] for t in self.targets})
        if not tickers:
            return
        try:
            markets: dict[str, KalshiMarket] = await self.kalshi.get_markets(tickers)
        except Exception as e:  # any outage: keep old quotes, which go stale -> no fair value
            self.store.log("kalshi_poll_failed", {"error": repr(e)[:300]})
            return
        fetched_at = self.clock()
        for ticker, m in markets.items():
            self._quotes[ticker] = KalshiQuote(market=m, fetched_at=fetched_at)

    def refresh_fair_values(self) -> None:
        now = self.clock()
        for t in self.targets:
            quote = self._quotes.get(t.map_row["kalshi_ticker"])
            self.fair_values.update(t.target.exchange_id, kalshi_fair_value(t.map_row, quote, now, self.fv_cfg))

    async def quote_once(self) -> None:
        self.refresh_fair_values()
        await self.quoter.cycle([t.target for t in self.targets], self.clock())

    async def reconcile_once(self) -> None:
        await self.reconciler.run_once()

    async def handle_halt_once(self) -> None:
        if not self.control.halted or self._halt_handled:
            return
        self._halt_handled = True
        reason = self.control.reason
        if self.shadow:
            self.store.log("kill_switch_shadow", {"reason": reason})
            self.alerter.send(f"Halted (shadow mode, nothing to cancel): {reason}")
            return
        result = await self.risk.kill_switch()
        self.alerter.send(
            f"Halted: {reason}. Kill switch "
            + ("clean: no open orders." if result.success else f"FAILED, still open: {result.remaining_order_ids}")
        )

    # --- loops ------------------------------------------------------------------------

    async def _periodic(self, name: str, interval: float, step: Callable[[], Awaitable[None]]) -> None:
        scheduled = self.mono()
        while True:
            self.looplag.record(name, scheduled)
            try:
                await step()
            except Exception as e:  # a loop must never die silently
                self.store.log("loop_error", {"loop": name, "error": repr(e)[:500]})
                self.alerter.send(f"{name} loop error: {e!r}"[:300])
            scheduled += interval
            await asyncio.sleep(max(0.0, scheduled - self.mono()))

    async def _halt_watcher(self) -> None:
        await self.control.wait_for_halt()
        await self.handle_halt_once()

    async def run(self, duration_seconds: float | None = None) -> None:
        tasks = [
            asyncio.create_task(self._periodic("kalshi_poll", float(self.settings["venues"]["kalshi"]["poll_interval_seconds"]),
                                               self.poll_kalshi_once)),  # fmt: skip
            asyncio.create_task(self._periodic("quoter", float(self.settings["quoter"]["cycle_interval_seconds"]),
                                               self.quote_once)),  # fmt: skip
            asyncio.create_task(self._periodic("reconciliation", float(self.settings["risk"]["reconciliation_interval_seconds"]),
                                               self.reconcile_once)),  # fmt: skip
            asyncio.create_task(self.looplag.run_probe()),
            asyncio.create_task(self._halt_watcher()),
        ] + [asyncio.create_task(f()) for f in self._extra_tasks]
        try:
            if duration_seconds is None:
                await asyncio.gather(*tasks)
            else:
                await asyncio.sleep(duration_seconds)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
