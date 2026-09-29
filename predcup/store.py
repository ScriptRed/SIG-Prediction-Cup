"""SQLite (WAL mode) persistence.

Only `events_log` is implemented here so far — CLAUDE.md's Hard Rule 8 and
the review that prompted this file only require somewhere real to log
markouts, quotes, fills, cancels and risk rejections. The rest of the
schema (markets, market_map, external_prices, fair_values, orders, fills,
positions) is its own unit of work, tracked separately in docs/PLAN.md.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class EventStore:
    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        # Single-row table: the size ramp's current step, so a restart can
        # resume one step below it instead of jumping back to full size or
        # all the way down to launch size (predcup.risk.SizeRamp).
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ramp_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                step INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def log(self, event_type: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO events_log (ts, event_type, payload) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), event_type, json.dumps(payload)),
        )
        self._conn.commit()

    def all_events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        if event_type is None:
            rows = self._conn.execute(
                "SELECT ts, event_type, payload FROM events_log ORDER BY id ASC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT ts, event_type, payload FROM events_log "
                "WHERE event_type = ? ORDER BY id ASC",
                (event_type,),
            ).fetchall()
        return [
            {"ts": ts, "event_type": et, "payload": json.loads(payload)}
            for ts, et, payload in rows
        ]

    def save_ramp_step(self, step: int) -> None:
        self._conn.execute(
            "INSERT INTO ramp_state (id, step, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET step = excluded.step, updated_at = excluded.updated_at",
            (step, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def load_ramp_step(self) -> int | None:
        row = self._conn.execute("SELECT step FROM ramp_state WHERE id = 1").fetchone()
        return None if row is None else int(row[0])

    def close(self) -> None:
        self._conn.close()
