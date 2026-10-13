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
from datetime import datetime, timedelta, timezone

from predcup.control import TradingControl
from predcup.risk import Alerter, PositionExposure
from predcup.store import EventStore, signed_fill_quantity

_TOLERANCE = 1e-6


@dataclass(frozen=True)
class ReconResult:
    status: str  # "clean" / "mismatch" / "read_failed"
    detail: str = ""


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
        clock: Callable[[], datetime] | None = None,
        fair_value_of: Callable[[str], float | None] | None = None,
        max_sig_spread: float = 0.05,
        clock_margin_seconds: float = 2.0,
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
        self._clock = clock or (lambda: datetime.now(timezone.utc))  # same clock as RiskManager
        self._markout_tasks: set[asyncio.Task] = set()  # kept referenced until done
        self.last_fill_sync_at: datetime | None = None  # PnlFeed's estimate needs recently synced fills
        self._clock_margin = clock_margin_seconds  # SIG fill timestamps vs our clock (NTP-synced)
        # Markouts: fresh polarity-adjusted Kalshi fair value (SIG YES terms)
        # when there is one, else the SIG mid only if the spread is under
        # max_sig_spread; a hollow book's mid is not a price.
        self._fair_value_of = fair_value_of or (lambda exchange_id: None)
        self._max_sig_spread = max_sig_spread

    def price_lookup(self, exchange_id: str) -> Callable[[int], Awaitable[tuple[float, str] | None]]:
        """Markout price and its source: fresh Kalshi fair value if any, else
        the SIG mid when SIG's spread is under max_sig_spread, else None
        (logged as markout_unavailable, never a made-up number)."""

        async def lookup(minutes: int) -> tuple[float, str] | None:
            fv = self._fair_value_of(exchange_id)
            if fv is not None:
                return fv, "kalshi_fair_value"
            tops = await self._venue.get_top_of_books([exchange_id], self._tid)
            bid, ask = tops.get(exchange_id, (None, None))
            if bid is None or ask is None or not (ask - bid < self._max_sig_spread - 1e-9):
                return None
            return (bid + ask) / 2, "sig_mid"

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

    def _diffs_as_of_read(
        self, venue_q: dict[str, float], read_started: datetime, read_ended: datetime
    ) -> dict[str, tuple[float, float]]:
        """exchange -> (local, venue) where no prefix of the fills that landed
        during the positions read explains the venue quantity. `local` is
        what all fills up to the end of the read add up to."""
        margin = timedelta(seconds=self._clock_margin)
        start, end = read_started - margin, read_ended + margin
        before: dict[str, float] = {}
        during: dict[str, list] = {}
        for f in self._store.fills(self._tid):
            if f.filled_at < start:
                before[f.exchange_id] = before.get(f.exchange_id, 0.0) + signed_fill_quantity(f)
            elif f.filled_at <= end:
                during.setdefault(f.exchange_id, []).append(f)
        diffs = {}
        for ex in set(venue_q) | set(before) | set(during):
            q = before.get(ex, 0.0)
            reachable = [q]
            for f in sorted(during.get(ex, []), key=lambda f: f.filled_at):
                q += signed_fill_quantity(f)
                reachable.append(q)
            v = venue_q.get(ex, 0.0)
            if not any(abs(v - r) <= _TOLERANCE for r in reachable):
                diffs[ex] = (reachable[-1], v)
        return diffs

    def pending_markouts(self) -> list[asyncio.Task]:
        return [t for t in self._markout_tasks if not t.done()]

    async def run_once(self) -> ReconResult:
        swept = self._router.swept_snapshot()  # before the fill sync, see OrderRouter.release_swept
        # Positions are compared as of the moment they were read (SIG's
        # positions read can take ~20 s while fills keep landing): read
        # positions first, then sync fills, then require each venue quantity
        # to equal every fill from before the read plus some prefix (in time
        # order) of the fills that landed during it. Later fills are ignored.
        # One recheck before declaring a mismatch.
        for attempt in (1, 2):
            try:
                # Taken BEFORE the read: a fill confirmed before this instant
                # is in these positions (RiskManager.update_positions).
                positions_as_of = read_started = self._clock()
                positions = await self._venue.get_positions(self._tid)
                read_ended = self._clock()
                await self._sync_fills()
                self.last_fill_sync_at = self._clock()
            except Exception as e:  # any read failure: not a mismatch, but not clean either
                return self._read_failed(repr(e)[:300])
            venue_q = {p.exchange_id: float(p.quantity) for p in positions if abs(p.quantity) > _TOLERANCE}
            diffs = self._diffs_as_of_read(venue_q, read_started, read_ended)
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
        self._risk.update_positions(exposures, as_of=positions_as_of)
        if not self._shadow:
            self._risk.record_reconciliation(True)
            self._router.release_swept(swept)
            self._router.clear_block("clean reconciliation")
        return ReconResult("clean")

    def _read_failed(self, detail: str) -> ReconResult:
        self._failures += 1
        self._store.log("reconciliation", {"status": "read_failed", "detail": detail, "consecutive": self._failures})
        self._alerter.send(f"Reconciliation read failed ({self._failures}/{self._max_failures}): {detail}")
        if self._failures >= self._max_failures and not self._shadow:
            self._control.halt(f"reconciliation failed to read {self._failures} times in a row")
        return ReconResult("read_failed", detail)
