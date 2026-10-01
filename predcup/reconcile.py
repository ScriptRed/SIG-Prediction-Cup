"""Reconciliation (CLAUDE.md): every reconciliation_interval_seconds, sync
new fills into the store (local positions are derived only from fills),
read /tournaments/{slug}/portfolio/positions, and compare.

- clean: RiskManager.record_reconciliation(True) (size ramp credit),
  positions fed into risk exposure, quotes swept since the last clean run
  released, a suspended router unblocked.
- mismatch: record_reconciliation(False) (ramp drops a step and alerts),
  halt trading (the halt handler cancels all and confirms), alert.
- read failure: alert; after max_read_failures in a row, halt.

In shadow mode the same reads and comparison run and mismatches alert,
but the ramp is never moved and nothing is halted: shadow runs place no
orders, so clean reconciliations must not grow the live size.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from predcup.control import TradingControl
from predcup.risk import Alerter, PositionExposure
from predcup.store import EventStore

_TOLERANCE = 1e-6


@dataclass(frozen=True)
class ReconResult:
    status: str  # "clean" / "mismatch" / "read_failed"
    detail: str = ""


def diff_positions(local: dict[str, float], venue: dict[str, float]) -> dict[str, tuple[float, float]]:
    """exchange_id -> (local, venue) wherever they differ; zero rows ignored."""
    out = {}
    for ex in set(local) | set(venue):
        a, b = local.get(ex, 0.0), venue.get(ex, 0.0)
        if abs(a - b) > _TOLERANCE:
            out[ex] = (a, b)
    return out


class Reconciler:
    def __init__(
        self,
        *,
        venue,  # SigVenue-like: get_new_fills, get_positions
        store: EventStore,
        risk,  # RiskManager-like: record_reconciliation, update_positions
        router,  # OrderRouter-like: swept_snapshot, release_swept, clear_block
        control: TradingControl,
        alerter: Alerter,
        tournament_id: str,
        market_meta: dict[str, tuple[str, str | None, str | None]],  # exchange_id -> (market_id, party, race_key)
        shadow: bool,
        max_read_failures: int = 3,
    ) -> None:
        self._venue = venue
        self._store = store
        self._risk = risk
        self._router = router
        self._control = control
        self._alerter = alerter
        self._tid = tournament_id
        self._meta = market_meta
        self._shadow = shadow
        self._max_failures = max_read_failures
        self._failures = 0
        self._markout_tasks: set[asyncio.Task] = set()  # kept referenced until done

    def price_lookup(self, exchange_id: str) -> Callable[[int], Awaitable[float | None]]:
        """Markout price: the SIG mid for that exchange, None unless two-sided."""

        async def lookup(minutes: int) -> float | None:
            tops = await self._venue.get_top_of_books([exchange_id], self._tid)
            bid, ask = tops.get(exchange_id, (None, None))
            return None if bid is None or ask is None else (bid + ask) / 2

        return lookup

    async def _sync_fills(self) -> None:
        known = {f.id for f in self._store.fills(self._tid)}
        for fill in await self._venue.get_new_fills(self._tid, known):
            if self._store.record_fill(fill):
                self._store.log("fill", {"fill_id": fill.id, "order_id": fill.order_id, "exchange_id": fill.exchange_id,
                                         "side": fill.side, "quantity": fill.quantity, "price": fill.price})  # fmt: skip
                # Every fill, the bot's or a manual one: 1/5/30-min markouts.
                # TODO(api): assumes Fill.price is YES-normalized (as for
                # orders); confirm with go-live gate (f).
                for task in self._risk.schedule_markouts(fill, self.price_lookup(fill.exchange_id)):
                    self._markout_tasks.add(task)
                    task.add_done_callback(self._markout_tasks.discard)

    def pending_markouts(self) -> list[asyncio.Task]:
        return [t for t in self._markout_tasks if not t.done()]

    async def run_once(self) -> ReconResult:
        swept = self._router.swept_snapshot()  # before the fill sync, see OrderRouter.release_swept
        # A fill can land between the fill sync and the positions read; that
        # looks like a mismatch. Sync and compare once more before declaring one.
        for attempt in (1, 2):
            try:
                await self._sync_fills()
                positions = await self._venue.get_positions(self._tid)
            except Exception as e:  # any read failure: not a mismatch, but not clean either
                return self._read_failed(repr(e)[:300])
            venue_q = {p.exchange_id: float(p.quantity) for p in positions if abs(p.quantity) > _TOLERANCE}
            local_q = {ex: float(q) for ex, q in self._store.local_positions(self._tid).items() if abs(q) > _TOLERANCE}
            diffs = diff_positions(local_q, venue_q)
            if not diffs:
                break
            if attempt == 1:
                self._store.log("reconciliation_recheck", {"diffs": {ex: list(v) for ex, v in diffs.items()}})
        self._failures = 0

        if diffs:
            detail = "; ".join(f"{ex}: local {a:g} vs venue {b:g}" for ex, (a, b) in sorted(diffs.items()))
            self._store.log("reconciliation", {"status": "mismatch", "detail": detail, "shadow": self._shadow})
            if self._shadow:
                self._alerter.send(f"Reconciliation mismatch (shadow, no action): {detail}")
            else:
                self._risk.record_reconciliation(False, detail)
                self._control.halt(f"reconciliation mismatch: {detail}")
                self._alerter.send(f"Reconciliation mismatch, halting and cancelling all: {detail}")
            return ReconResult("mismatch", detail)

        exposures = []
        for p in positions:
            if abs(p.quantity) <= _TOLERANCE:
                continue
            market_id, party, race = self._meta.get(p.exchange_id, (p.market_id, None, None))
            price = p.current_price if p.current_price is not None else p.avg_cost
            exposures.append(PositionExposure(market_id=market_id, party_id=party, race_key=race,
                                              quantity=float(p.quantity), price=float(price)))  # fmt: skip
        self._store.log("reconciliation", {"status": "clean", "positions": len(exposures), "shadow": self._shadow})
        self._risk.update_positions(exposures)
        await self._feed_pnl()
        if not self._shadow:
            self._risk.record_reconciliation(True)
            self._router.release_swept(swept)
            self._router.clear_block("clean reconciliation")
        return ReconResult("clean")

    async def _feed_pnl(self) -> None:
        """Today's Cup P&L -> the daily loss stop; account value -> bankroll
        (caps are fractions of it). A null P&L or a failed read changes
        nothing and is logged; it never reads as 0."""
        get_pnl = getattr(self._venue, "get_pnl", None)
        if get_pnl is None:
            return
        try:
            pnl = await get_pnl(self._tid, period="day")
        except Exception as e:
            self._store.log("pnl_read_failed", {"error": repr(e)[:300]})
            return
        self._risk.update_bankroll(pnl.total_account_value)
        if pnl.period_pnl is not None:
            self._risk.update_daily_pnl(pnl.period_pnl)
        self._store.log("pnl", {"day_pnl": pnl.period_pnl, "account_value": pnl.total_account_value})

    def _read_failed(self, detail: str) -> ReconResult:
        self._failures += 1
        self._store.log("reconciliation", {"status": "read_failed", "detail": detail, "consecutive": self._failures})
        self._alerter.send(f"Reconciliation read failed ({self._failures}/{self._max_failures}): {detail}")
        if self._failures >= self._max_failures and not self._shadow:
            self._control.halt(f"reconciliation failed to read {self._failures} times in a row")
        return ReconResult("read_failed", detail)
