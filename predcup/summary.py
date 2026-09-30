"""Daily Telegram summary at daily_summary.time (08:00) in
daily_summary.timezone (Europe/London, so BST/GMT are handled): P&L, fills,
markouts, ramp level and alerts over the last 24 h.

    reporter = DailySummaryReporter.from_config(settings, store=store,
        tournament_id=tid, status_provider=AppStatusProvider(app),
        alerter=alerter, pnl_total=None)   # async () -> float, when available
    app.add_task(reporter.run)

Sent at most once per UK calendar day: each attempt is logged as a
`daily_summary` event with that date. A bot that was down at 08:00 sends
it when it comes back up the same day. The P&L line needs `pnl_total`
(total Cup P&L; no reader for /tournaments/{slug}/portfolio/pnl in sig.py
yet); the change is measured against the previous summary's total.
Everything is read from SQLite (indexed) plus one StatusProvider snapshot.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from predcup.status import StatusProvider, format_ramp
from predcup.store import EventStore

logger = logging.getLogger(__name__)

SUMMARY_HEADER = "predcup daily summary"
WINDOW = timedelta(hours=24)
MAX_CHECK_INTERVAL_SECONDS = 60.0
ALERT_PREVIEW_CHARS = 120


class Alerter(Protocol):
    def send(self, message: str) -> None: ...


@dataclass(frozen=True)
class DailySummaryConfig:
    time: str  # "HH:MM", local to `timezone`
    timezone: str
    max_alerts_listed: int

    def __post_init__(self) -> None:
        _parse_time(self.time)
        ZoneInfo(self.timezone)
        if self.max_alerts_listed < 0:
            raise ValueError("daily_summary.max_alerts_listed must be >= 0")


def load_daily_summary_config(config: dict) -> DailySummaryConfig:
    section = config["daily_summary"]
    return DailySummaryConfig(
        time=str(section["time"]),
        timezone=section["timezone"],
        max_alerts_listed=int(section["max_alerts_listed"]),
    )


def _parse_time(hhmm: str) -> time:
    hours, minutes = hhmm.split(":")
    return time(int(hours), int(minutes))


def _run_at_on(day: date, hhmm: str, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, _parse_time(hhmm), tzinfo=tz).astimezone(timezone.utc)


def next_run_at(now: datetime, hhmm: str, timezone_name: str) -> datetime:
    """The first `hhmm` local time strictly after `now`, in UTC."""
    tz = ZoneInfo(timezone_name)
    local_day = now.astimezone(tz).date()
    candidate = _run_at_on(local_day, hhmm, tz)
    if candidate <= now:
        candidate = _run_at_on(local_day + timedelta(days=1), hhmm, tz)
    return candidate


class DailySummaryReporter:
    def __init__(
        self,
        *,
        store: EventStore,
        tournament_id: str,
        status_provider: StatusProvider,
        alerter: Alerter,
        time_of_day: str,
        timezone_name: str,
        max_alerts_listed: int,
        pnl_total: Callable[[], Awaitable[float]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._tid = tournament_id
        self._status = status_provider
        self._alerter = alerter
        self._time = time_of_day
        self._tz = ZoneInfo(timezone_name)
        self._max_alerts = max_alerts_listed
        self._pnl_total = pnl_total
        self._clock = clock
        self._sleep = sleep

    @classmethod
    def from_config(cls, config: dict, **kwargs) -> DailySummaryReporter:
        cfg = load_daily_summary_config(config)
        return cls(time_of_day=cfg.time, timezone_name=cfg.timezone, max_alerts_listed=cfg.max_alerts_listed,
                   **kwargs)  # fmt: skip

    # --- scheduling -------------------------------------------------------------------

    def _due_date(self, now: datetime) -> date | None:
        """Today's local date if today's run time has passed, else None."""
        local_day = now.astimezone(self._tz).date()
        return local_day if now >= _run_at_on(local_day, self._time, self._tz) else None

    def _already_done(self, day: date) -> bool:
        last = self._store.latest_event("daily_summary")
        return last is not None and last["payload"].get("date") == day.isoformat()

    async def send_if_due(self) -> bool:
        """Send today's summary if its time has passed and it hasn't been
        attempted today. Returns True if one was sent."""
        day = self._due_date(self._clock())
        if day is None or self._already_done(day):
            return False
        try:
            pnl_total = await self._read_pnl_total()
            text = await self.compose(day=day, pnl_total=pnl_total)
        except Exception as exc:
            logger.exception("daily summary failed")
            # Logged as attempted so a persistent failure alerts once, not every minute.
            self._store.log("daily_summary", {"date": day.isoformat(), "status": "failed", "error": repr(exc)[:300]})
            self._alerter.send(f"Daily summary failed: {exc!r}"[:300])
            return False
        self._store.log("daily_summary", {"date": day.isoformat(), "status": "sent", "pnl_total": pnl_total})
        self._alerter.send(text)
        return True

    async def run(self) -> None:
        while True:
            await self.send_if_due()
            now = self._clock()
            wait = (next_run_at(now, self._time, self._tz.key) - now).total_seconds()
            # Short sleeps: re-read the clock often so a suspended VM or a
            # clock step doesn't push the summary out by hours.
            await self._sleep(max(1.0, min(MAX_CHECK_INTERVAL_SECONDS, wait)))

    # --- content ----------------------------------------------------------------------

    async def _read_pnl_total(self) -> float | None:
        if self._pnl_total is None:
            return None
        try:
            return float(await asyncio.wait_for(self._pnl_total(), 10.0))
        except Exception as exc:
            logger.warning("daily summary: P&L read failed: %r", exc)
            return None

    async def compose(self, day: date | None = None, pnl_total: float | None = None) -> str:
        now = self._clock()
        since = now - WINDOW
        day = day or now.astimezone(self._tz).date()
        if pnl_total is None:
            pnl_total = await self._read_pnl_total()
        snap = await self._status.snapshot()
        lines = [
            f"{SUMMARY_HEADER} {day.isoformat()} ({snap.mode}), last 24 h",
            self._pnl_line(pnl_total),
            self._fills_line(since, now),
            self._markouts_line(since),
            f"Size ramp: {format_ramp(snap.ramp_step, snap.ramp_max_step, snap.ramp_multiplier)}",
            f"Trading: HALTED: {snap.halt_reason}" if snap.halted else "Trading: running",
        ]
        lines += self._alert_lines(since)
        return "\n".join(lines)

    def _pnl_line(self, pnl_total: float | None) -> str:
        if pnl_total is None:
            return "P&L: n/a"
        previous = None
        for e in self._store.recent_events("daily_summary", limit=10):
            if e["payload"].get("pnl_total") is not None:
                previous = float(e["payload"]["pnl_total"])
                break
        line = f"P&L: total {pnl_total:+,.2f}"
        if previous is not None:
            line += f", since last summary {pnl_total - previous:+,.2f}"
        return line

    def _fills_line(self, since: datetime, now: datetime) -> str:
        fills = [f for f in self._store.fills(self._tid) if since <= f.filled_at <= now]
        if not fills:
            return "Fills: 0"
        shares = sum(f.quantity for f in fills)
        markets = len({f.exchange_id for f in fills})
        return f"Fills: {len(fills)} ({shares} shares, {markets} markets)"

    def _markouts_line(self, since: datetime) -> str:
        by_horizon: dict[int, list[float]] = defaultdict(list)
        for e in self._store.events_since("markout", since):
            by_horizon[int(e["payload"]["minutes"])].append(float(e["payload"]["markout"]))
        if not by_horizon:
            return "Markouts: none"
        parts = [
            f"{m}m {sum(v) / len(v):+.3f} (n={len(v)})" for m, v in sorted(by_horizon.items())
        ]
        return "Markouts (mean per share): " + ", ".join(parts)

    def _alert_lines(self, since: datetime) -> list[str]:
        alerts = [
            e for e in self._store.events_since("alert", since)
            if not str(e["payload"].get("message", "")).startswith(SUMMARY_HEADER)
        ]  # fmt: skip
        lines = [f"Alerts (24h): {len(alerts)}"]
        shown = alerts[-self._max_alerts:] if self._max_alerts else []
        for e in shown:
            message = " ".join(str(e["payload"].get("message", "")).split())
            if len(message) > ALERT_PREVIEW_CHARS:
                message = message[: ALERT_PREVIEW_CHARS - 3] + "..."
            lines.append(f"- {e['ts'][11:16]} {message}")
        if len(alerts) > len(shown):
            lines.append(f"  ({len(alerts) - len(shown)} earlier not shown)")
        return lines
