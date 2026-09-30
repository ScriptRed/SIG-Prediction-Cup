"""Bot status for Telegram /status (and the daily summary's ramp line).

A StatusProvider returns a StatusSnapshot; format_status() renders it.
AppStatusProvider is the ready-made provider over predcup.app.App:

    provider = AppStatusProvider(app, pnl_today=some_async_fn)  # pnl optional
    router = CommandRouter(..., status_provider=provider)

P&L has no reader yet (the Cup's /tournaments/{slug}/portfolio/pnl is not
in sig.py), so pnl_today is an injected async callable; without one,
/status shows "n/a". Any value that can't be read shows "n/a", never 0.
All reads are small: one venue call (open orders, with a timeout), one
local positions query, indexed events_log reads.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from predcup.risk import load_size_ramp_config, max_ramp_step

logger = logging.getLogger(__name__)

LOOP_LAG_EVENTS_SCANNED = 200  # newest loop_lag events read for the latest value per loop
VENUE_READ_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class ReconciliationInfo:
    status: str  # clean | mismatch | read_failed
    at: datetime


@dataclass(frozen=True)
class StatusSnapshot:
    as_of: datetime
    mode: str  # "shadow" | "live"
    halted: bool
    halt_reason: str
    ramp_step: int
    ramp_max_step: int
    ramp_multiplier: float
    open_orders: int | None
    positions: int | None
    pnl_today: float | None
    last_reconciliation: ReconciliationInfo | None
    loop_lag: dict[str, float] = field(default_factory=dict)  # latest lag per loop, seconds


class StatusProvider(Protocol):
    async def snapshot(self) -> StatusSnapshot: ...


def _na(value: object) -> str:
    return "n/a" if value is None else str(value)


def format_ramp(step: int, max_step: int, multiplier: float) -> str:
    if multiplier >= 1.0:
        return f"step {step}/{max_step} (full size)"
    return f"step {step}/{max_step} ({multiplier:.0%} of full size)"


def format_status(s: StatusSnapshot) -> str:
    trading = f"HALTED: {s.halt_reason}" if s.halted else "running"
    pnl = "n/a" if s.pnl_today is None else f"{s.pnl_today:+,.2f}"
    if s.last_reconciliation is None:
        recon = "none yet"
    else:
        age = (s.as_of - s.last_reconciliation.at).total_seconds()
        recon = f"{s.last_reconciliation.status}, {age:.0f}s ago"
    if s.loop_lag:
        worst = max(s.loop_lag.values())
        detail = ", ".join(f"{name} {lag:.2f}s" for name, lag in sorted(s.loop_lag.items()))
        lag = f"max {worst:.2f}s ({detail})"
    else:
        lag = "n/a"
    return "\n".join([
        f"predcup {s.mode.upper()} status, {s.as_of:%Y-%m-%d %H:%M:%S} UTC",
        f"Trading: {trading}",
        f"Size ramp: {format_ramp(s.ramp_step, s.ramp_max_step, s.ramp_multiplier)}",
        f"Open orders: {_na(s.open_orders)}",
        f"Positions: {_na(s.positions)}",
        f"P&L today: {pnl}",
        f"Last reconciliation: {recon}",
        f"Loop lag: {lag}",
    ])  # fmt: skip


def latest_loop_lags(store, limit: int = LOOP_LAG_EVENTS_SCANNED) -> dict[str, float]:
    out: dict[str, float] = {}
    for e in store.recent_events("loop_lag", limit=limit):  # newest first
        loop = e["payload"].get("loop")
        if loop is not None and loop not in out:
            out[loop] = float(e["payload"]["lag_seconds"])
    return out


def latest_reconciliation(store) -> ReconciliationInfo | None:
    e = store.latest_event("reconciliation")
    if e is None:
        return None
    return ReconciliationInfo(status=str(e["payload"].get("status")), at=datetime.fromisoformat(e["ts"]))


class AppStatusProvider:
    """StatusProvider over App's public attributes (ramp, control, risk,
    venue, store, tid, shadow, settings)."""

    def __init__(
        self,
        app,
        pnl_today: Callable[[], Awaitable[float]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._app = app
        self._pnl_today = pnl_today
        self._clock = clock
        self._max_step = max_ramp_step(load_size_ramp_config(app.settings))

    async def snapshot(self) -> StatusSnapshot:
        app = self._app
        halted = app.control.halted or app.risk.is_killed
        reason = app.control.reason or ("kill switch engaged" if app.risk.is_killed else "")
        return StatusSnapshot(
            as_of=self._clock(),
            mode="shadow" if app.shadow else "live",
            halted=halted,
            halt_reason=reason,
            ramp_step=app.ramp.step,
            ramp_max_step=self._max_step,
            ramp_multiplier=app.ramp.multiplier,
            open_orders=await self._open_orders(),
            positions=self._positions(),
            pnl_today=await self._pnl(),
            last_reconciliation=latest_reconciliation(app.store),
            loop_lag=latest_loop_lags(app.store),
        )

    async def _open_orders(self) -> int | None:
        try:
            orders = await asyncio.wait_for(
                self._app.venue.get_open_orders(self._app.tid), VENUE_READ_TIMEOUT_SECONDS
            )
        except Exception as exc:
            logger.warning("status: open orders read failed: %r", exc)
            return None
        return len(orders)

    def _positions(self) -> int | None:
        try:
            return sum(1 for q in self._app.store.local_positions(self._app.tid).values() if q)
        except Exception as exc:
            logger.warning("status: positions read failed: %r", exc)
            return None

    async def _pnl(self) -> float | None:
        if self._pnl_today is None:
            return None
        try:
            return float(await asyncio.wait_for(self._pnl_today(), VENUE_READ_TIMEOUT_SECONDS))
        except Exception as exc:
            logger.warning("status: P&L read failed: %r", exc)
            return None
