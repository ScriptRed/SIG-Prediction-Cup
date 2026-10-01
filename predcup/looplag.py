"""Loop-lag metric: how late each loop iteration runs versus its schedule.

The trading loop must never block (CLAUDE.md "Process split"): anything
heavier than per-market arithmetic runs in a separate process and hands
results over via SQLite. This module is how we notice if that rule is
broken. Two sources feed it:

- `record(loop, scheduled_at)`: called at the top of each iteration of a
  periodic loop (quoter, reconciliation) with the monotonic time the
  iteration was due.
- `run_probe()`: a background task that sleeps `probe_interval_seconds`
  and records how late it woke up — any blocking call anywhere on the
  asyncio event loop shows up here as `loop="event_loop"`.

Every measurement is logged to events_log as `loop_lag`. Lag above
`alert_threshold_seconds` alerts, at most once per `alert_cooldown_seconds`
across all loops (so a bad patch can't flood Telegram); the next alert
says how many slow readings were suppressed and the worst one.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from predcup.store import EventStore

EVENT_LOOP = "event_loop"


class Alerter(Protocol):
    def send(self, message: str) -> None: ...


@dataclass(frozen=True)
class LoopLagConfig:
    alert_threshold_seconds: float
    alert_cooldown_seconds: float
    probe_interval_seconds: float

    def __post_init__(self) -> None:
        if self.alert_threshold_seconds <= 0:
            raise ValueError("loop_lag.alert_threshold_seconds must be > 0")
        if self.alert_cooldown_seconds < 0:
            raise ValueError("loop_lag.alert_cooldown_seconds must be >= 0")
        if self.probe_interval_seconds <= 0:
            raise ValueError("loop_lag.probe_interval_seconds must be > 0")


def load_loop_lag_config(config: dict) -> LoopLagConfig:
    section = config["loop_lag"]
    return LoopLagConfig(
        alert_threshold_seconds=section["alert_threshold_seconds"],
        alert_cooldown_seconds=section["alert_cooldown_seconds"],
        probe_interval_seconds=section["probe_interval_seconds"],
    )


class LoopLagMonitor:
    def __init__(
        self,
        config: LoopLagConfig,
        event_store: EventStore,
        alerter: Alerter,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._event_store = event_store
        self._alerter = alerter
        self._clock = clock
        # One cooldown across all loops (2026-10-01): a bad patch that slows
        # every loop at once sends one alert, not one per loop. Slow readings
        # inside the cooldown are counted and summarized in the next alert.
        self._last_alert_at: float | None = None
        self._suppressed = 0
        self._worst_suppressed: tuple[float, str] | None = None

    def record(self, loop: str, scheduled_at: float) -> float:
        """Log how late `loop` started relative to `scheduled_at` (same
        clock as this monitor, monotonic by default). Returns the lag."""
        now = self._clock()
        lag = max(0.0, now - scheduled_at)
        over = lag > self._config.alert_threshold_seconds
        alerted = False
        if over:
            last = self._last_alert_at
            if last is None or now - last >= self._config.alert_cooldown_seconds:
                self._last_alert_at = now
                alerted = True
                message = (
                    f"Loop lag: {loop} ran {lag:.1f}s late "
                    f"(threshold {self._config.alert_threshold_seconds:g}s). "
                    "Something is blocking the trading loop."
                )
                if self._suppressed:
                    worst_lag, worst_loop = self._worst_suppressed  # type: ignore[misc]
                    message += (f" {self._suppressed} more slow readings suppressed since the last alert,"
                                f" worst {worst_lag:.1f}s ({worst_loop}).")  # fmt: skip
                self._suppressed, self._worst_suppressed = 0, None
                self._alerter.send(message)
            else:
                self._suppressed += 1
                if self._worst_suppressed is None or lag > self._worst_suppressed[0]:
                    self._worst_suppressed = (lag, loop)
        self._event_store.log(
            "loop_lag",
            {"loop": loop, "lag_seconds": round(lag, 4), "over_threshold": over, "alerted": alerted},
        )
        return lag

    async def probe_once(
        self, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    ) -> float:
        interval = self._config.probe_interval_seconds
        scheduled_at = self._clock() + interval
        await sleep(interval)
        return self.record(EVENT_LOOP, scheduled_at)

    async def run_probe(self) -> None:
        while True:
            await self.probe_once()
