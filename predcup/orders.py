"""The order router: the only code path from a strategy to a venue's
place_order / place_batch (CLAUDE.md hard rule 1; enforced structurally by
tests/test_order_path.py).

replace_all() is the re-quote primitive (CLAUDE.md platform rules):
tournament-scoped cancel-all -> confirm via GET /orders?status=open that
nothing remains -> risk.check() every new order -> post the approved ones
in batches of <= 50. Orders never stack: if anything is still open after
cancel-all, nothing is posted and a person is alerted.

Shadow mode runs the same risk checks and logs every order it would have
sent (`shadow_quote`), but never cancels or places anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from predcup.control import TradingControl
from predcup.fairvalue import FairValue
from predcup.models import Order, OrderStatus
from predcup.risk import Alerter
from predcup.store import EventStore
from predcup.venues.base import Venue
from predcup.venues.sig import (
    MAX_BATCH,
    OrderStatusUnknown,
    SigApiError,
    SigOrderRejected,
    new_idempotency_key,
)


@dataclass(frozen=True)
class RouterResult:
    approved: int = 0
    placed: int = 0
    shadow: bool = False
    blocked: str = ""


class OrderRouter:
    def __init__(
        self,
        *,
        venue: Venue,
        risk,  # RiskManager (duck-typed so tests can pass a denying fake)
        store: EventStore,
        tournament_id: str,
        shadow: bool,
        alerter: Alerter,
        control: TradingControl,
    ) -> None:
        self._venue = venue
        self._risk = risk
        self._store = store
        self._tid = tournament_id
        self._shadow = shadow
        self._alerter = alerter
        self._control = control
        self._live_keys: list[str] = []  # risk tracking keys of our resting quotes
        self._blocked = ""

    @property
    def shadow(self) -> bool:
        return self._shadow

    @property
    def blocked(self) -> str:
        """Non-empty while quoting is suspended pending reconciliation."""
        return self._blocked

    def clear_block(self, reason: str) -> None:
        """Called by the reconciliation loop after a clean reconciliation."""
        if self._blocked:
            self._store.log("router_unblocked", {"was": self._blocked, "reason": reason})
        self._blocked = ""

    def _block(self, reason: str) -> None:
        self._blocked = reason
        self._store.log("router_blocked", {"reason": reason})
        self._alerter.send(f"Quoting suspended until reconciliation: {reason}")

    def _approve(self, orders_with_fv: list[tuple[Order, FairValue]], now: datetime) -> list[tuple[Order, FairValue]]:
        approved = []
        for order, fv in orders_with_fv:
            if not fv.ok or fv.value is None or fv.as_of is None:
                raise ValueError(f"order {order.idempotency_key} has no usable fair value ({fv.reason})")
            age = (now - fv.as_of).total_seconds()
            decision = self._risk.check(order, fair_value=fv.value, outside_data_age_seconds=age)
            if decision.approved:
                approved.append((order, fv))
        return approved

    async def replace_all(self, orders_with_fv: list[tuple[Order, FairValue]], now: datetime) -> RouterResult:
        if self._control.halted:
            return RouterResult(blocked=f"halted: {self._control.reason}")

        if self._shadow:
            approved = self._approve(orders_with_fv, now)
            self._store.log("shadow_cancel_all", {"tournament_id": self._tid})
            for order, fv in approved:
                self._store.log("shadow_quote", _order_payload(order, fv))
            return RouterResult(approved=len(approved), shadow=True)

        if self._blocked:
            return RouterResult(blocked=self._blocked)

        # 1. Pull everything we have resting, and confirm.
        try:
            result = await self._venue.cancel_all(self._tid)
            self._store.log("cancel_all", {"cancelled": result.cancelled, "remaining": result.remaining})
            still_open = await self._venue.get_open_orders(self._tid)
        except SigApiError as e:
            self._block(f"cancel-all failed: {e}")
            return RouterResult(blocked=self._blocked)
        if still_open:
            ids = [o.id for o in still_open]
            msg = f"{len(ids)} order(s) remain open after cancel-all: {ids}; not re-posting"
            self._store.log("cancel_all_incomplete", {"remaining_order_ids": ids})
            self._alerter.send(msg)
            return RouterResult(blocked=msg)
        for key in self._live_keys:
            self._risk.confirm_order_state(key, OrderStatus.CANCELLED)
        self._live_keys = []

        # 2. Risk-check and record each new order before it is sent, so
        # orders in the same batch count against each other's limits.
        approved = []
        for order, fv in self._approve(orders_with_fv, now):
            self._risk.record_order(order)  # PENDING, keyed by idempotency key
            approved.append(order)

        # 3. Post in batches of <= 50, a fresh key per batch.
        placed = 0
        for i in range(0, len(approved), MAX_BATCH):
            chunk = approved[i : i + MAX_BATCH]
            batch_key = new_idempotency_key()
            try:
                results = await self._venue.place_batch(chunk, batch_key)
            except OrderStatusUnknown:
                # Orders stay PENDING (exposure counted) until reconciliation.
                self._store.log("batch_status_unknown", {"batch_key": batch_key, "keys": [o.idempotency_key for o in chunk]})
                self._block(f"batch {batch_key} status unknown (ORDER_STATUS_UNKNOWN)")
                break
            except SigOrderRejected as e:
                for o in chunk:
                    self._risk.confirm_order_state(o.idempotency_key, OrderStatus.REJECTED)
                self._store.log("batch_rejected", {"batch_key": batch_key, "status": e.status, "code": e.code, "message": e.message})
                self._control.halt(f"whole batch rejected: {e.status} {e.code} {e.message}")
                self._alerter.send(f"Trading halted: whole batch rejected ({e.status} {e.code}: {e.message})")
                break
            except SigApiError as e:
                # Retries exhausted (429/503): some items may be live.
                self._store.log("batch_incomplete", {"batch_key": batch_key, "status": e.status, "code": e.code})
                self._block(f"batch {batch_key} incomplete after retries ({e.status} {e.code})")
                break
            for r in results:
                key = r.order.idempotency_key
                if r.ok:
                    self._risk.confirm_order_state(key, r.order.status)
                    if r.order.status in (OrderStatus.OPEN, OrderStatus.PENDING):
                        self._live_keys.append(key)
                    placed += 1
                    self._store.log("order", {**_order_payload(r.order, None), "order_id": r.order.id,
                                              "status": r.order.status.value})  # fmt: skip
                else:
                    self._risk.confirm_order_state(key, OrderStatus.REJECTED)
                    self._store.log("order_failed", {"key": key, "status": r.status, "code": r.code, "message": r.message})
                    if 400 <= r.status < 500 and r.status != 429 and r.order.market_id:
                        self._risk.record_order_rejection(r.order.market_id, r.status, r.code, r.message)
        return RouterResult(approved=len(approved), placed=placed)


def _order_payload(order: Order, fv: FairValue | None) -> dict:
    return {
        "key": order.idempotency_key,
        "exchange_id": order.exchange_id,
        "market_id": order.market_id,
        "race_key": order.race_key,
        "party_id": order.party_id,
        "side": order.side,
        "action": order.action,
        "quantity": order.quantity,
        "price": order.price,
        "expiration_date": order.expiration_date.isoformat() if order.expiration_date else None,
        "fair_value": fv.value if fv else None,
        "uncertainty": fv.uncertainty if fv else None,
    }
