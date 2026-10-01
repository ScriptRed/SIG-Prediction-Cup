"""Daily P&L for the loss stop, in its own task (2026-10-01).

SIG's portfolio P&L read takes 0.2-18.5 s under load (VM measurement), so it
must never hold up quoting or reconciliation. PnlFeed.run_once():

1. Reads SIG's day P&L (SigVenue.get_pnl, 45 s portfolio timeout). If it
   has a value (direct or derived), that feeds the loss stop and becomes the
   estimate's base: local positions, marks and known fills at that moment.
2. Otherwise (timeout, error, or null and not derivable) it estimates the
   day P&L locally: base P&L + mark-to-market change of the base positions
   + new fills since the base marked to market. Marks are fresh Kalshi fair
   value, else a tight SIG mid (App._pnl_marks); an unmarked market adds no
   change. The estimate needs a base and recently synced fills.
3. If neither is available the loss stop gets None: RiskManager refuses new
   orders in live mode (fail closed). One alert per outage, one on recovery.

Accounting is YES-signed: a NO fill is a YES sale at its YES-normalized
price, so holding YES and buying NO (netting) stays linear.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from predcup.models import Fill
from predcup.risk import Alerter
from predcup.store import EventStore, signed_fill_quantity


def estimate_day_pnl(
    base_pnl: float,
    base_positions: dict[str, float],  # exchange -> signed YES shares at the base
    base_marks: dict[str, float | None],
    new_fills: list[Fill],  # fills since the base, prices YES-normalized
    marks_now: dict[str, float | None],
) -> float:
    est = base_pnl
    for ex, q in base_positions.items():
        before, now = base_marks.get(ex), marks_now.get(ex)
        if before is not None and now is not None:
            est += q * (now - before)
    for f in new_fills:
        now = marks_now.get(f.exchange_id)
        if now is not None:
            est += signed_fill_quantity(f) * (now - f.price)
    return est


class PnlFeed:
    def __init__(
        self,
        *,
        venue,  # SigVenue-like: get_pnl(tournament_id, period)
        store: EventStore,
        risk,  # RiskManager-like: update_daily_pnl, update_bankroll
        alerter: Alerter,
        tournament_id: str,
        shadow: bool,
        marks: Callable[[list[str]], Awaitable[dict[str, float | None]]],
        fills_fresh: Callable[[], bool],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._venue = venue
        self._store = store
        self._risk = risk
        self._alerter = alerter
        self._tid = tournament_id
        self._shadow = shadow
        self._marks = marks
        self._fills_fresh = fills_fresh
        self._clock = clock
        self._base: tuple[float, dict[str, float], dict[str, float | None], set[str]] | None = None
        self._missing = False

    async def run_once(self) -> str:
        """Returns the source fed to risk: "sig", "estimate" or "unavailable"."""
        try:
            pnl = await self._venue.get_pnl(self._tid, period="day")
            self._risk.update_bankroll(pnl.total_account_value)
            value, sig_detail = pnl.period_pnl, "periodPnl null and not derivable"
        except Exception as e:  # timeout or error: fall back to the estimate
            value, sig_detail = None, f"read failed: {e!r}"[:200]

        if value is not None:
            await self._rebase(value)
            return self._feed(value, "sig")
        estimate = await self._estimate()
        if estimate is not None:
            return self._feed(estimate, "estimate", sig_detail)
        self._risk.update_daily_pnl(None)
        self._store.log("pnl_unavailable", {"detail": sig_detail, "estimate": "no base or fills not fresh"})
        if not self._missing:
            self._missing = True
            effect = "logged only (shadow)" if self._shadow else "new orders refused until it is back"
            self._alerter.send(f"Daily P&L unavailable ({sig_detail}; no local estimate): daily loss stop "
                               f"can't be checked; {effect}.")  # fmt: skip
        return "unavailable"

    def _feed(self, value: float, source: str, sig_detail: str = "") -> str:
        self._risk.update_daily_pnl(value)
        self._store.log("pnl", {"day_pnl": round(value, 2), "source": source, "sig_detail": sig_detail})
        if self._missing:
            self._missing = False
            self._alerter.send(f"Daily P&L available again ({value:+,.2f}, {source}); daily loss stop active.")
        return source

    def _positions(self) -> dict[str, float]:
        return {ex: float(q) for ex, q in self._store.local_positions(self._tid).items() if q}

    async def _rebase(self, value: float) -> None:
        positions = self._positions()
        marks = await self._marks(sorted(positions)) if positions else {}
        known = {f.id for f in self._store.fills(self._tid)}
        self._base = (value, positions, marks, known)

    async def _estimate(self) -> float | None:
        if self._base is None or not self._fills_fresh():
            return None
        base_pnl, base_positions, base_marks, known = self._base
        new_fills = [f for f in self._store.fills(self._tid) if f.id not in known]
        exchanges = sorted(set(base_positions) | {f.exchange_id for f in new_fills})
        marks_now = await self._marks(exchanges) if exchanges else {}
        return estimate_day_pnl(base_pnl, base_positions, base_marks, new_fills, marks_now)
