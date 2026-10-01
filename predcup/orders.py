"""The order router: the only code path from a strategy to a venue's
place_order / place_batch (CLAUDE.md hard rule 1; enforced structurally by
tests/test_order_path.py).

requote() is the re-quote primitive (CLAUDE.md platform rules): cancel-all
scoped to each re-quoted market's exchangeId -> confirm via one GET
/orders?status=open -> risk.check() every new order -> post the approved
ones in batches of <= 50. Orders never stack: a market that still shows
open orders after its cancel is not re-posted, and a person is alerted.
The router never cancels tournament-wide: that is only the kill switch
and shutdown (RiskManager.kill_switch), so manual orders in markets the
bot doesn't quote survive re-quotes.

Shadow mode runs the same risk checks and logs every order it would have
sent (`shadow_quote`), but never cancels or places anything.
"""

from __future__ import annotations

import asyncio
import time
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
    done: frozenset[str] = frozenset()  # markets whose re-quote went through


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
        self._live_keys: dict[str, list[str]] = {}  # exchange_id -> risk keys of our resting quotes
        self._swept_keys: list[str] = []  # cancelled by cancel-all, not yet reconciled
        self._blocked = ""
        self._inflight = 0  # live requote/test-order calls in progress
        self.placement_count = 0  # orders that reached the venue; the kill path compares it

    def _halted(self) -> bool:
        return self._control.halted or bool(getattr(self._risk, "is_killed", False))

    async def wait_idle(self, timeout_seconds: float) -> bool:
        """True once no live requote is in flight (False on timeout). The kill
        path waits on this, then sweeps again if anything landed meanwhile."""
        deadline = time.monotonic() + timeout_seconds
        while self._inflight:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

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

    def swept_snapshot(self) -> list[str]:
        """Keys swept so far; the reconciler takes this BEFORE syncing fills."""
        return list(self._swept_keys)

    def release_swept(self, keys: list[str]) -> None:
        """After a clean reconciliation (fills now in positions), stop counting
        the quotes that were swept before it started."""
        done = set(keys)
        for key in keys:
            self._risk.confirm_order_state(key, OrderStatus.CANCELLED)
        self._swept_keys = [k for k in self._swept_keys if k not in done]

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

    async def requote(
        self, updates: dict[str, list[tuple[Order, FairValue]]], now: datetime
    ) -> RouterResult:
        """Re-quote the markets in `updates` (exchange_id -> new orders; an
        empty list pulls that market). Each market is cancelled on its own
        (cancel-all scoped by exchangeId), so manual orders elsewhere on the
        account are never touched. One open-orders read confirms the sweep;
        a market that still shows open orders is not re-posted (alert), the
        others go out in batches of <= 50."""
        if self._control.halted:
            return RouterResult(blocked=f"halted: {self._control.reason}")
        all_orders = [ofv for orders in updates.values() for ofv in orders]

        if self._shadow:
            approved = self._approve(all_orders, now)
            for ex in updates:
                self._store.log("shadow_cancel", {"exchange_id": ex})
            for order, fv in approved:
                self._store.log("shadow_quote", _order_payload(order, fv))
            return RouterResult(approved=len(approved), shadow=True, done=frozenset(updates))

        if self._blocked:
            return RouterResult(blocked=self._blocked)
        if not updates:
            return RouterResult()
        self._inflight += 1
        try:
            return await self._requote_live(updates, now)
        finally:
            self._inflight -= 1

    async def _cancel_late(self, landed: list[Order]) -> None:
        """A halt arrived while these orders were in flight: cancel each by id
        (never tournament-wide here), stop counting them, log."""
        for o in landed:
            if o.id:
                try:
                    await self._venue.cancel(o.id, self._tid)
                except Exception as e:  # the kill path's sweep is the backstop
                    self._store.log("late_cancel_failed", {"order_id": o.id, "error": repr(e)[:200]})
                    continue
            self._risk.confirm_order_state(o.idempotency_key, OrderStatus.CANCELLED)
        self._store.log("late_orders_cancelled", {"count": len(landed), "order_ids": [o.id for o in landed]})

    async def _requote_live(
        self, updates: dict[str, list[tuple[Order, FairValue]]], now: datetime
    ) -> RouterResult:
        # 1. Cancel each re-quoted market, then confirm with one read.
        try:
            for ex in updates:
                result = await self._venue.cancel_all(self._tid, exchange_id=ex)
                self._store.log("cancel", {"exchange_id": ex, "cancelled": result.cancelled, "remaining": result.remaining})
            still_open = await self._venue.get_open_orders(self._tid)
        except SigApiError as e:
            self._block(f"cancel failed: {e}")
            return RouterResult(blocked=self._blocked)
        stuck = {o.exchange_id for o in still_open if o.exchange_id in updates}
        if stuck:
            ids = [o.id for o in still_open if o.exchange_id in stuck]
            self._store.log("cancel_incomplete", {"exchange_ids": sorted(stuck), "remaining_order_ids": ids})
            self._alerter.send(f"{len(ids)} order(s) remain open after cancel in {sorted(stuck)}; not re-posting there")
        done = frozenset(ex for ex in updates if ex not in stuck)
        for ex in (ex for ex in updates if ex in done):  # input order: deterministic batches
            # Swept quotes keep counting (they may have filled first) until a
            # clean reconciliation has moved any fills into positions.
            self._swept_keys += self._live_keys.pop(ex, [])

        # 2. Risk-check and record each new order before it is sent, so
        # orders in the same batch count against each other's limits.
        approved = []
        for order, fv in self._approve([ofv for ex in updates if ex in done for ofv in updates[ex]], now):
            self._risk.record_order(order)  # PENDING, keyed by idempotency key
            approved.append(order)

        # 3. Post in batches of <= 50, a fresh key per batch.
        placed = 0
        unknown_items = 0
        for i in range(0, len(approved), MAX_BATCH):
            chunk = approved[i : i + MAX_BATCH]
            if self._halted():  # a kill landed while earlier chunks were in flight
                for o in approved[i:]:
                    self._risk.confirm_order_state(o.idempotency_key, OrderStatus.REJECTED)
                break
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
            landed = [r.order for r in results if r.ok]
            self.placement_count += len(landed)
            if self._halted():
                await self._cancel_late(landed)
                for r in results:
                    if not r.ok:
                        self._risk.confirm_order_state(r.order.idempotency_key, OrderStatus.REJECTED)
                for o in approved[i + MAX_BATCH :]:
                    self._risk.confirm_order_state(o.idempotency_key, OrderStatus.REJECTED)
                break
            for r in results:
                key = r.order.idempotency_key
                if r.ok:
                    self._risk.confirm_order_state(key, r.order.status)
                    if r.order.status in (OrderStatus.OPEN, OrderStatus.PENDING):
                        self._live_keys.setdefault(r.order.exchange_id, []).append(key)
                    placed += 1
                    self._store.log("order", {**_order_payload(r.order, None), "order_id": r.order.id,
                                              "status": r.order.status.value})  # fmt: skip
                elif r.status >= 500:
                    # Outcome unknown (e.g. per-item ORDER_STATUS_UNKNOWN after the
                    # adapter's retries): it may be live. Keep it PENDING (counted)
                    # and stop quoting until reconciliation has seen the truth.
                    self._store.log("order_status_unknown", {"key": key, "status": r.status, "code": r.code,
                                                             "message": r.message})  # fmt: skip
                    unknown_items += 1
                else:
                    self._risk.confirm_order_state(key, OrderStatus.REJECTED)
                    self._store.log("order_failed", {"key": key, "status": r.status, "code": r.code, "message": r.message})
                    if 400 <= r.status < 500 and r.status != 429 and r.order.market_id:
                        self._risk.record_order_rejection(r.order.market_id, r.status, r.code, r.message)
        if unknown_items:
            self._block(f"{unknown_items} batch item(s) with unknown outcome (5xx)")
        return RouterResult(approved=len(approved), placed=placed, done=done, blocked=self._blocked)


    async def place_test_order(self, order: Order) -> Order | None:
        """Go-live gate (c)/(d) only: one order through
        RiskManager.check_test_order (1 share, extreme price, every other
        limit). Returns the placed order, or None if refused."""
        if self._control.halted:
            return None
        decision = self._risk.check_test_order(order)
        if not decision.approved:
            return None
        self._risk.record_order(order)
        self._inflight += 1
        try:
            [result] = await self._venue.place_batch([order], new_idempotency_key())
        finally:
            self._inflight -= 1
        if result.ok:
            self.placement_count += 1
        if not result.ok:
            self._risk.confirm_order_state(order.idempotency_key, OrderStatus.REJECTED)
            self._store.log("test_order_failed", {"status": result.status, "code": result.code, "message": result.message})
            return None
        self._risk.confirm_order_state(order.idempotency_key, result.order.status)
        self._store.log("test_order", {**_order_payload(result.order, None), "order_id": result.order.id})
        return result.order


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
