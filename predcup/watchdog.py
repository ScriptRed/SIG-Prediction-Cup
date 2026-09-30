"""systemd watchdog and crash recovery (deploy/predcup.service: Type=notify,
WatchdogSec).

Wiring for main.py / App:

    watchdog = Watchdog.from_config(settings, alerter)
    # in App._periodic, at the top of each iteration next to looplag.record:
    watchdog.beat(name)
    app.add_task(watchdog.run)
    # first thing once App exists, before the loops start:
    await cancel_all_on_startup(app)

Watchdog.run() sends READY=1, then WATCHDOG=1 every ping interval (a third
of systemd's WatchdogSec) but only while every loop in `watchdog.loops` has
beaten within `max_silence_seconds`. A hung loop stops the pings (and
alerts once); a blocked event loop stops them too, since run() can't be
scheduled. Either way systemd kills and restarts the bot after WatchdogSec,
and the restarted bot's cancel_all_on_startup() clears whatever the hung
process left resting.

sd_notify is the plain datagram protocol (man sd_notify), no dependency;
it does nothing when NOTIFY_SOCKET is unset (tests, running by hand).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from predcup.risk import KillSwitchResult

logger = logging.getLogger(__name__)


class Alerter(Protocol):
    def send(self, message: str) -> None: ...


def notify_socket_address(path: str) -> str:
    """'@name' is an abstract-namespace socket: leading NUL instead."""
    return "\0" + path[1:] if path.startswith("@") else path


def sd_notify(message: str, env: Mapping[str, str] | None = None) -> bool:
    """Send one sd_notify message. False if not under systemd or on error;
    never raises (a failed ping must not take the bot down, systemd will)."""
    path = (os.environ if env is None else env).get("NOTIFY_SOCKET")
    if not path:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(notify_socket_address(path))
            sock.sendall(message.encode())
        return True
    except OSError as exc:
        logger.warning("sd_notify(%s) failed: %s", message, exc)
        return False


def watchdog_ping_interval(
    env: Mapping[str, str] | None = None, *, default: float, pid: int | None = None
) -> float:
    """A third of systemd's WatchdogSec (WATCHDOG_USEC), else `default`."""
    env = os.environ if env is None else env
    usec = env.get("WATCHDOG_USEC")
    if not usec:
        return default
    target_pid = env.get("WATCHDOG_PID")
    if target_pid and int(target_pid) != (os.getpid() if pid is None else pid):
        return default
    return int(usec) / 1_000_000 / 3


@dataclass(frozen=True)
class WatchdogConfig:
    loops: tuple[str, ...]
    max_silence_seconds: float
    default_ping_interval_seconds: float

    def __post_init__(self) -> None:
        if self.max_silence_seconds <= 0:
            raise ValueError("watchdog.max_silence_seconds must be > 0")
        if self.default_ping_interval_seconds <= 0:
            raise ValueError("watchdog.default_ping_interval_seconds must be > 0")


def load_watchdog_config(config: dict) -> WatchdogConfig:
    section = config["watchdog"]
    return WatchdogConfig(
        loops=tuple(section["loops"]),
        max_silence_seconds=float(section["max_silence_seconds"]),
        default_ping_interval_seconds=float(section["default_ping_interval_seconds"]),
    )


class Watchdog:
    def __init__(
        self,
        loops: Iterable[str],
        max_silence_seconds: float,
        ping_interval_seconds: float,
        alerter: Alerter,
        notify: Callable[[str], bool] = sd_notify,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._max_silence = max_silence_seconds
        self._interval = ping_interval_seconds
        self._alerter = alerter
        self._notify = notify
        self._clock = clock
        self._sleep = sleep
        start = clock()
        # A loop that never beats goes stale max_silence after start.
        self._last_beat: dict[str, float] = {name: start for name in loops}
        self._ready_sent = False
        self._alerted = False

    @classmethod
    def from_config(cls, config: dict, alerter: Alerter) -> Watchdog:
        cfg = load_watchdog_config(config)
        interval = watchdog_ping_interval(default=cfg.default_ping_interval_seconds)
        return cls(cfg.loops, cfg.max_silence_seconds, interval, alerter)

    def beat(self, loop: str) -> None:
        """Call at the top of every iteration of a watched loop."""
        if loop in self._last_beat:
            self._last_beat[loop] = self._clock()

    def stale_loops(self) -> list[str]:
        now = self._clock()
        return sorted(n for n, t in self._last_beat.items() if now - t > self._max_silence)

    def tick(self) -> bool:
        """One ping decision. Returns True if WATCHDOG=1 was sent."""
        if not self._ready_sent:
            self._notify("READY=1")
            self._ready_sent = True
        stale = self._stale_with_age()
        if stale:
            if not self._alerted:
                self._alerted = True
                detail = ", ".join(f"{n} silent {age:.0f}s" for n, age in stale)
                self._alerter.send(
                    f"Watchdog: {detail} (limit {self._max_silence:g}s). Withholding the "
                    "systemd ping: the bot will be restarted and cancel all Cup orders on start."
                )
            return False
        self._alerted = False
        self._notify("WATCHDOG=1")
        return True

    def _stale_with_age(self) -> list[tuple[str, float]]:
        now = self._clock()
        return [(n, now - self._last_beat[n]) for n in self.stale_loops()]

    async def run(self) -> None:
        try:
            while True:
                self.tick()
                await self._sleep(self._interval)
        finally:
            self._notify("STOPPING=1")


async def cancel_all_on_startup(app, retry_delay_seconds: float = 1.0) -> KillSwitchResult | None:
    """Clear anything a crashed or hung predecessor left resting: a
    tournament-wide cancel-all confirmed via open orders
    (RiskManager.kill_switch). Live mode only: in shadow mode any resting
    order is a manual one. If orders remain, the bot halts (App.request_kill)
    and alerts; it never starts quoting over orders it can't account for.

    `app` needs .shadow, .risk, .store, .alerter and .request_kill.
    """
    if app.shadow:
        app.store.log("startup_cancel_all", {"result": "skipped (shadow)"})
        return None
    result = await app.risk.kill_switch(retry_delay_seconds=retry_delay_seconds)
    if result.success:
        app.store.log("startup_cancel_all", {"result": "clean", "attempts": result.attempts})
        return result
    app.store.log("startup_cancel_all", {"result": "failed", "remaining_order_ids": result.remaining_order_ids})
    app.alerter.send(
        f"Startup cancel-all FAILED: {len(result.remaining_order_ids)} Cup order(s) still open "
        f"{result.remaining_order_ids}. Halted; check the venue by hand."
    )
    app.request_kill("startup cancel-all failed")
    return result
