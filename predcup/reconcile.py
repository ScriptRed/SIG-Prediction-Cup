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

    async def run_once(self) -> ReconResult:
        swept = self._router.swept_snapshot()  # before the fill sync, see OrderRouter.release_swept
        try:
            known = {f.id for f in self._store.fills(self._tid)}
            for fill in await self._venue.get_new_fills(self._tid, known):
                if self._store.record_fill(fill):
                    self._store.log("fill", {"fill_id": fill.id, "order_id": fill.order_id, "exchange_id": fill.exchange_id,
                                             "side": fill.side, "quantity": fill.quantity, "price": fill.price})  # fmt: skip
            positions = await self._venue.get_positions(self._tid)
        except Exception as e:  # any read failure: not a mismatch, but not clean either
            return self._read_failed(repr(e)[:300])
        self._failures = 0

        venue_q = {p.exchange_id: float(p.quantity) for p in positions if abs(p.quantity) > _TOLERANCE}
        local_q = {ex: float(q) for ex, q in self._store.local_positions(self._tid).items() if abs(q) > _TOLERANCE}
        diffs = diff_positions(local_q, venue_q)

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
