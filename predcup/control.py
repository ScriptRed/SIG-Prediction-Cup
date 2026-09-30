"""Process-wide trading halt. Anything may halt (KILL file watcher,
Telegram /kill, a whole-batch rejection); only a person resumes. The
quoter and the order router check it before every action; main.py runs
the kill switch (cancel all, confirm) when it flips."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone


class TradingControl:
    def __init__(self) -> None:
        self._reason = ""
        self._since: datetime | None = None
        self._event = asyncio.Event()

    @property
    def halted(self) -> bool:
        return bool(self._reason)

    @property
    def reason(self) -> str:
        return self._reason

    def halt(self, reason: str) -> None:
        """Idempotent; the first reason is kept."""
        if not self._reason:
            self._reason = reason or "halted"
            self._since = datetime.now(timezone.utc)
        self._event.set()

    async def wait_for_halt(self) -> str:
        await self._event.wait()
        return self._reason
