"""KILL file watcher (CLAUDE.md Hard Rule 7): a file named KILL in the repo
root cancels all orders and stops quoting.

    watcher = KillFileWatcher.from_config(config, risk.kill, alerter=alerter)
    asyncio.create_task(watcher.run())

Polls rather than using inotify so it works the same on every filesystem
(WSL, network mounts). A file already present at startup kills
immediately. The callback fires once per appearance; delete and re-create
the file to fire it again. If the callback raises (venue unreachable), the
kill is not marked done and is retried on the next poll.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent


class Alerter(Protocol):
    def send(self, message: str) -> None: ...


@dataclass(frozen=True)
class KillFileConfig:
    path: Path
    poll_interval_seconds: float

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError("kill_switch.poll_interval_seconds must be > 0")


def load_kill_file_config(config: dict, repo_root: Path = REPO_ROOT) -> KillFileConfig:
    section = config["kill_switch"]
    path = Path(section["file_path"])
    if not path.is_absolute():
        path = repo_root / path
    return KillFileConfig(path=path, poll_interval_seconds=section["poll_interval_seconds"])


class KillFileWatcher:
    def __init__(
        self,
        path: Path,
        on_kill: Callable[[str], Awaitable[object]],
        poll_interval_seconds: float,
        alerter: Alerter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._path = Path(path)
        self._on_kill = on_kill
        self._poll_interval = poll_interval_seconds
        self._alerter = alerter
        self._sleep = sleep
        self._fired = False

    @classmethod
    def from_config(
        cls,
        config: dict,
        on_kill: Callable[[str], Awaitable[object]],
        alerter: Alerter | None = None,
    ) -> KillFileWatcher:
        loaded = load_kill_file_config(config)
        return cls(loaded.path, on_kill, loaded.poll_interval_seconds, alerter=alerter)

    async def check_once(self) -> bool:
        """Poll the file once. Returns True if it fired the kill this call."""
        if not self._path.exists():
            self._fired = False
            return False
        if self._fired:
            return False
        try:
            await self._on_kill(f"KILL file {self._path}")
        except Exception as exc:
            logger.exception("kill via KILL file failed; retrying next poll")
            if self._alerter is not None:
                self._alerter.send(f"KILL file seen but kill raised {exc!r}; retrying.")
            return False
        self._fired = True
        return True

    async def run(self) -> None:
        while True:
            await self.check_once()
            await self._sleep(self._poll_interval)
