"""SQLite (WAL mode) persistence.

Tables:
- `events_log`: every quote, order, fill, cancel, fair-value change, risk
  rejection and ramp change (CLAUDE.md conventions).
- `ramp_state`: the size ramp's current step (predcup.risk.SizeRamp).
- `orders`: every order we have submitted, keyed by idempotency key (the
  venue order id arrives only after acceptance).
- `fills`: every fill, keyed by the venue's fill id so the same fill
  arriving from the websocket and a REST resync is stored once.
- `positions`: local signed position per (tournament, exchange), derived
  only from fills — the local side of reconciliation against
  /tournaments/{slug}/portfolio/positions.

Still open (docs/PLAN.md step 1): markets, market_map, external_prices,
fair_values.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from predcup.models import Fill, Order, OrderStatus

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS events_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    # Single-row table: the size ramp's current step, so a restart can
    # resume one step below it instead of jumping back to full size or
    # all the way down to launch size (predcup.risk.SizeRamp).
    """
    CREATE TABLE IF NOT EXISTS ramp_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        step INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        idempotency_key TEXT PRIMARY KEY,
        order_id TEXT UNIQUE,
        tournament_id TEXT NOT NULL,
        exchange_id TEXT NOT NULL,
        market_id TEXT,
        party_id TEXT,
        race_key TEXT,
        side TEXT NOT NULL,
        action TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        price REAL,
        expiration_date TEXT,
        status TEXT NOT NULL,
        created_at TEXT,
        terminal_reason_code TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS orders_by_status ON orders (tournament_id, status)",
    """
    CREATE TABLE IF NOT EXISTS fills (
        fill_id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL,
        tournament_id TEXT NOT NULL,
        exchange_id TEXT NOT NULL,
        side TEXT NOT NULL,
        action TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        price REAL NOT NULL,
        filled_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS fills_by_tournament ON fills (tournament_id, filled_at)",
    """
    CREATE TABLE IF NOT EXISTS positions (
        tournament_id TEXT NOT NULL,
        exchange_id TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (tournament_id, exchange_id)
    )
    """,
)

_ORDER_COLUMNS = (
    "idempotency_key", "order_id", "tournament_id", "exchange_id", "market_id",
    "party_id", "race_key", "side", "action", "quantity", "price",
    "expiration_date", "status", "created_at", "terminal_reason_code",
)  # fmt: skip


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat()


def _parse(ts: str | None) -> datetime | None:
    return None if ts is None else datetime.fromisoformat(ts)


def signed_fill_quantity(fill: Fill) -> int:
    """Signed YES-share change from one fill: + = towards YES, − = towards
    NO, matching the platform's Position.quantity sign. Buying NO while
    holding YES therefore nets the position down share-for-share."""
    towards_yes = (fill.side == "yes") == (fill.action == "buy")
    return fill.quantity if towards_yes else -fill.quantity


class EventStore:
    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        for statement in _SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    # --- events_log ---------------------------------------------------------

    def log(self, event_type: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO events_log (ts, event_type, payload) VALUES (?, ?, ?)",
            (_now_iso(), event_type, json.dumps(payload)),
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

    # --- ramp_state ---------------------------------------------------------

    def save_ramp_step(self, step: int) -> None:
        self._conn.execute(
            "INSERT INTO ramp_state (id, step, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET step = excluded.step, updated_at = excluded.updated_at",
            (step, _now_iso()),
        )
        self._conn.commit()

    def load_ramp_step(self) -> int | None:
        row = self._conn.execute("SELECT step FROM ramp_state WHERE id = 1").fetchone()
        return None if row is None else int(row[0])

    # --- orders -------------------------------------------------------------

    def upsert_order(self, order: Order) -> None:
        values = (
            order.idempotency_key, order.id, order.tournament_id, order.exchange_id,
            order.market_id, order.party_id, order.race_key, order.side, order.action,
            order.quantity, order.price, _iso(order.expiration_date), order.status.value,
            _iso(order.created_at), order.terminal_reason_code,
        )  # fmt: skip
        placeholders = ", ".join("?" for _ in _ORDER_COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in _ORDER_COLUMNS[1:])
        self._conn.execute(
            f"INSERT INTO orders ({', '.join(_ORDER_COLUMNS)}, updated_at) "
            f"VALUES ({placeholders}, ?) "
            f"ON CONFLICT(idempotency_key) DO UPDATE SET {updates}, updated_at = excluded.updated_at",
            (*values, _now_iso()),
        )
        self._conn.commit()

    def _order_from_row(self, row: tuple) -> Order:
        r = dict(zip(_ORDER_COLUMNS, row))
        return Order(
            id=r["order_id"],
            exchange_id=r["exchange_id"],
            market_id=r["market_id"],
            tournament_id=r["tournament_id"],
            party_id=r["party_id"],
            race_key=r["race_key"],
            side=r["side"],
            action=r["action"],
            quantity=r["quantity"],
            price=r["price"],
            expiration_date=_parse(r["expiration_date"]),
            idempotency_key=r["idempotency_key"],
            status=OrderStatus(r["status"]),
            created_at=_parse(r["created_at"]),
            terminal_reason_code=r["terminal_reason_code"],
        )

    def _select_orders(self, where: str, params: tuple) -> list[Order]:
        rows = self._conn.execute(
            f"SELECT {', '.join(_ORDER_COLUMNS)} FROM orders WHERE {where} ORDER BY rowid ASC",
            params,
        ).fetchall()
        return [self._order_from_row(r) for r in rows]

    def get_order(self, idempotency_key: str) -> Order | None:
        found = self._select_orders("idempotency_key = ?", (idempotency_key,))
        return found[0] if found else None

    def get_order_by_venue_id(self, order_id: str) -> Order | None:
        found = self._select_orders("order_id = ?", (order_id,))
        return found[0] if found else None

    def orders_with_status(
        self, tournament_id: str, statuses: Iterable[OrderStatus]
    ) -> list[Order]:
        status_values = [s.value for s in statuses]
        placeholders = ", ".join("?" for _ in status_values)
        return self._select_orders(
            f"tournament_id = ? AND status IN ({placeholders})",
            (tournament_id, *status_values),
        )

    # --- fills and positions ------------------------------------------------

    def record_fill(self, fill: Fill) -> bool:
        """Store a fill and apply it to the local position in one
        transaction. Returns False (and changes nothing) if this fill id
        was already recorded."""
        with self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO fills (fill_id, order_id, tournament_id, exchange_id, "
                "side, action, quantity, price, filled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fill.id, fill.order_id, fill.tournament_id, fill.exchange_id, fill.side,
                    fill.action, fill.quantity, fill.price, fill.filled_at.isoformat(),
                ),  # fmt: skip
            )
            if cur.rowcount == 0:
                return False
            self._conn.execute(
                "INSERT INTO positions (tournament_id, exchange_id, quantity, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(tournament_id, exchange_id) DO UPDATE SET "
                "quantity = quantity + excluded.quantity, updated_at = excluded.updated_at",
                (fill.tournament_id, fill.exchange_id, signed_fill_quantity(fill), _now_iso()),
            )
        return True

    def fills(self, tournament_id: str) -> list[Fill]:
        rows = self._conn.execute(
            "SELECT fill_id, order_id, exchange_id, tournament_id, side, action, quantity, "
            "price, filled_at FROM fills WHERE tournament_id = ? ORDER BY filled_at, rowid",
            (tournament_id,),
        ).fetchall()
        return [
            Fill(
                id=r[0], order_id=r[1], exchange_id=r[2], tournament_id=r[3], side=r[4],
                action=r[5], quantity=r[6], price=r[7], filled_at=datetime.fromisoformat(r[8]),
            )  # fmt: skip
            for r in rows
        ]

    def local_positions(self, tournament_id: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT exchange_id, quantity FROM positions WHERE tournament_id = ?",
            (tournament_id,),
        ).fetchall()
        return {exchange_id: quantity for exchange_id, quantity in rows}

    def replay_positions_from_fills(self, tournament_id: str) -> dict[str, int]:
        """Recompute positions from the fills table alone (consistency check
        for the positions cache)."""
        out: dict[str, int] = {}
        for fill in self.fills(tournament_id):
            out[fill.exchange_id] = out.get(fill.exchange_id, 0) + signed_fill_quantity(fill)
        return out

    def close(self) -> None:
        self._conn.close()
