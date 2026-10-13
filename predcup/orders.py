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

Cancel volume (2026-10-13 live run: 429s on cancel-all): scoped cancel-alls
are paced (CancelPacer: at most orders.cancel_all_max_per_window per
orders.cancel_all_window_seconds, markets over budget wait for a later
cycle, in the quoter's priority order). A 429 pauses every re-quote until
its Retry-After has passed; what was already cancelled is posted at once
with a fresh expiry, and quotes priced more than
orders.max_quote_age_seconds ago are never posted. The kill switch is
never paced.

Shadow mode runs the same risk checks and logs every order it would have
sent (`shadow_quote`), but never cancels or places anything.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from predcup.control import TradingControl
from predcup.fairvalue import FairValue
from predcup.models import Order, OrderStatus, invalid_reason
from predcup.risk import Alerter
from predcup.store import EventStore
from predcup.venues.base import Venue
from predcup.venues.sig import (
    MAX_BATCH,
    OrderStatusUnknown,
    SigApiError,
    SigOrderRejected,
    SigRateLimited,
    new_idempotency_key,
)


@dataclass(frozen=True)
class SendRules:
    """expirationDate = send time + quote_expiration_seconds, stamped right
    before each batch goes out (after the cancels, however long they took);
    nothing is sent expiring sooner than min_expiry_margin_seconds."""

    quote_expiration_seconds: float
    min_expiry_margin_seconds: float
    cancel_all_max_per_window: int  # scoped cancel-alls per sliding window
    cancel_all_window_seconds: float
    max_quote_age_seconds: float  # quotes priced longer ago than this are not posted
    rate_limit_pause_seconds: float  # pause after a 429 that carried no Retry-After


def load_send_rules(settings: dict) -> SendRules:
    o = settings["orders"]
    rules = SendRules(
        quote_expiration_seconds=float(settings["quoter"]["quote_expiration_seconds"]),
        min_expiry_margin_seconds=float(o["min_expiry_margin_seconds"]),
        cancel_all_max_per_window=int(o["cancel_all_max_per_window"]),
        cancel_all_window_seconds=float(o["cancel_all_window_seconds"]),
        max_quote_age_seconds=float(o["max_quote_age_seconds"]),
        rate_limit_pause_seconds=float(o["rate_limit_pause_seconds"]),
    )
    if rules.quote_expiration_seconds <= rules.min_expiry_margin_seconds:
        raise ValueError("quoter.quote_expiration_seconds must exceed orders.min_expiry_margin_seconds")
    if rules.cancel_all_max_per_window < 1 or rules.cancel_all_window_seconds <= 0:
        raise ValueError("orders.cancel_all_max_per_window must be >= 1 and the window positive")
    return rules


class CancelPacer:
    """Sliding-window budget for scoped cancel-alls, plus a pause (a 429's
    Retry-After) during which nothing is taken."""

    def __init__(self, max_calls: int, window_seconds: float) -> None:
        self._max = max_calls
        self._window = timedelta(seconds=window_seconds)
        self._calls: deque[datetime] = deque()
        self._paused_until: datetime | None = None

    def pause_until(self, t: datetime) -> None:
        if self._paused_until is None or t > self._paused_until:
            self._paused_until = t

    def paused_until(self, now: datetime) -> datetime | None:
        return self._paused_until if self._paused_until is not None and now < self._paused_until else None

    def take(self, now: datetime) -> bool:
        if self.paused_until(now) is not None:
            return False
        while self._calls and now - self._calls[0] >= self._window:
            self._calls.popleft()
        if len(self._calls) >= self._max:
            return False
        self._calls.append(now)
        return True


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RouterResult:
    approved: int = 0
    placed: int = 0
    shadow: bool = False
    blocked: str = ""
    done: frozenset[str] = frozenset()  # markets swept by this re-quote (cancelled and confirmed)
    deferred: frozenset[str] = frozenset()  # not cancelled this cycle (pacing / Retry-After)
    expires: dict[str, datetime] = field(default_factory=dict)  # done market -> expiry of its resting quotes


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
        send_rules: SendRules,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._venue = venue
        self._rules = send_rules
        self._clock = clock
        self._pacer = CancelPacer(send_rules.cancel_all_max_per_window, send_rules.cancel_all_window_seconds)
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

    def _refused(self, order: Order, now: datetime | None = None) -> bool:
        """Orders the router never sends, whatever risk would say (models.
        invalid_reason): market orders, off-tick or out-of-range prices,
        quantities below one share, no expirationDate (CLAUDE.md: every
        resting quote has a short expiry, the dead-man's switch) and, given
        `now`, an expiry already past or within the margin."""
        reason = invalid_reason(order, now, self._rules.min_expiry_margin_seconds)
        if reason:
            self._store.log("order_refused", {"key": order.idempotency_key, "exchange_id": order.exchange_id,
                                              "market_id": order.market_id, "reason": reason,
                                              "order": _order_payload(order, None)})  # fmt: skip
        return bool(reason)

    def _stamp_and_gate(self, orders: list[Order]) -> list[Order]:
        """Right before a batch goes out: expirationDate = now + TTL (never
        the cycle start: a cancel can sit out a long Retry-After), then the
        final validity gate. Refused orders are forgotten by risk."""
        now = self._clock()
        expiry = now + timedelta(seconds=self._rules.quote_expiration_seconds)
        out = []
        for o in orders:
            o = o.model_copy(update={"expiration_date": expiry})
            if self._refused(o, now):
                self._risk.confirm_order_state(o.idempotency_key, OrderStatus.REJECTED)
                continue
            out.append(o)
        return out

    def _approve(self, orders_with_fv: list[tuple[Order, FairValue]], now: datetime) -> list[tuple[Order, FairValue]]:
        approved = []
        for order, fv in orders_with_fv:
            if self._refused(order):
                continue
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
            expiry = self._clock() + timedelta(seconds=self._rules.quote_expiration_seconds)
            return RouterResult(approved=len(approved), shadow=True, done=frozenset(updates),
                                expires={o.exchange_id: expiry for o, _ in approved})  # fmt: skip

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
        # 1. Cancel every re-quoted market concurrently (a slow market can't
        # delay the others), then confirm with one read. A market whose cancel
        # failed or timed out is not re-posted this cycle: its old quotes may
        # still rest, so nothing stacks; the next cycle tries it again.
        # Paced: markets over the cancel budget (or all, during a 429 pause)
        # wait for a later cycle, in the caller's priority order.
        started = self._clock()
        markets: list[str] = []
        deferred: list[str] = []
        for ex in updates:
            (markets if self._pacer.take(started) else deferred).append(ex)
        if deferred:
            paused = self._pacer.paused_until(started)
            self._store.log("requote_deferred", {
                "markets": deferred,
                "reason": f"rate limited until {paused.isoformat()}" if paused else "cancel-all pacing",
            })  # fmt: skip
        if not markets:
            return RouterResult(deferred=frozenset(deferred))
        results = await asyncio.gather(*(self._venue.cancel_all(self._tid, exchange_id=ex) for ex in markets),
                                       return_exceptions=True)  # fmt: skip
        failed: set[str] = set()
        for ex, result in zip(markets, results):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result  # cancellation etc.
                failed.add(ex)
                if isinstance(result, SigRateLimited):
                    self._rate_limited(result, "cancel-all", ex)
                self._store.log("cancel_failed", {"exchange_id": ex, "error": repr(result)[:200]})
            else:
                self._store.log("cancel", {"exchange_id": ex, "cancelled": result.cancelled, "remaining": result.remaining})
        try:
            still_open = await self._venue.get_open_orders(self._tid)
        except SigApiError as e:
            self._block(f"open-orders confirm read failed: {e}")
            return RouterResult(blocked=self._blocked)
        stuck = {o.exchange_id for o in still_open if o.exchange_id in markets} | failed
        if stuck - failed:
            ids = [o.id for o in still_open if o.exchange_id in stuck]
            self._store.log("cancel_incomplete", {"exchange_ids": sorted(stuck), "remaining_order_ids": ids})
            self._alerter.send(f"{len(ids)} order(s) remain open after cancel in {sorted(stuck)}; not re-posting there")
        done = frozenset(ex for ex in markets if ex not in stuck)
        for ex in (ex for ex in markets if ex in done):  # input order: deterministic batches
            # Swept quotes keep counting (they may have filled first) until a
            # clean reconciliation has moved any fills into positions.
            self._swept_keys += self._live_keys.pop(ex, [])

        # 2. Risk-check and record each new order before it is sent, so
        # orders in the same batch count against each other's limits.
        approved = []
        for order, fv in self._approve([ofv for ex in markets if ex in done for ofv in updates[ex]], now):
            self._risk.record_order(order)  # PENDING, keyed by idempotency key
            approved.append(order)

        # 3. Post in batches of <= 50, a fresh key per batch.
        # Quotes were priced at `now`; if the cancels took too long (slow
        # venue, retries), they are not posted: the next cycle re-prices.
        placed = 0
        unknown_items = 0
        expires: dict[str, datetime] = {}
        for i in range(0, len(approved), MAX_BATCH):
            if self._halted():  # a kill landed while earlier chunks were in flight
                for o in approved[i:]:
                    self._risk.confirm_order_state(o.idempotency_key, OrderStatus.REJECTED)
                break
            age = (self._clock() - now).total_seconds()
            if age > self._rules.max_quote_age_seconds:
                for o in approved[i:]:
                    self._risk.confirm_order_state(o.idempotency_key, OrderStatus.REJECTED)
                self._store.log("requote_stale", {"age_seconds": round(age, 3), "markets": sorted({o.exchange_id for o in approved[i:]}),
                                                  "max_quote_age_seconds": self._rules.max_quote_age_seconds})  # fmt: skip
                break
            chunk = self._stamp_and_gate(approved[i : i + MAX_BATCH])
            if not chunk:
                continue
            batch_key = new_idempotency_key()
            try:
                results = await self._venue.place_batch(chunk, batch_key)
            except OrderStatusUnknown:
                # Orders stay PENDING (exposure counted) until reconciliation.
                self._store.log("batch_status_unknown", {"batch_key": batch_key, "keys": [o.idempotency_key for o in chunk]})
                self._block(f"batch {batch_key} status unknown (ORDER_STATUS_UNKNOWN)")
                break
            except SigRateLimited as e:
                # Every item rate-limited: nothing placed. Forget them, pause.
                for o in approved[i:]:
                    self._risk.confirm_order_state(o.idempotency_key, OrderStatus.REJECTED)
                self._store.log("batch_rate_limited", {"batch_key": batch_key, "retry_after": e.retry_after,
                                                       "keys": [o.idempotency_key for o in chunk]})  # fmt: skip
                self._rate_limited(e, "batch", None)
                break
            except SigOrderRejected as e:
                for o in chunk:
                    self._risk.confirm_order_state(o.idempotency_key, OrderStatus.REJECTED)
                self._store.log("batch_rejected", {"batch_key": batch_key, "status": e.status, "code": e.code,
                                                   "message": e.message, "details": e.details,
                                                   "orders": [_order_payload(o, None) for o in chunk]})  # fmt: skip
                self._control.halt(f"whole batch rejected: {e.status} {e.code} {e.message}")
                detail = f" {json.dumps(e.details)[:300]}" if e.details else ""
                self._alerter.send(f"Trading halted: whole batch rejected ({e.status} {e.code}: {e.message}){detail}")
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
                        if r.order.expiration_date is not None:
                            expires[r.order.exchange_id] = r.order.expiration_date
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
                    self._store.log("order_failed", {"key": key, "status": r.status, "code": r.code, "message": r.message,
                                                     "details": r.details, "order": _order_payload(r.order, None)})  # fmt: skip
                    if r.status == 429:
                        self._rate_limited(None, "batch item", r.order.exchange_id)
                    elif 400 <= r.status < 500 and r.order.market_id:
                        self._risk.record_order_rejection(r.order.market_id, r.status, r.code, r.message)
        if unknown_items:
            self._block(f"{unknown_items} batch item(s) with unknown outcome (5xx)")
        return RouterResult(approved=len(approved), placed=placed, done=done, blocked=self._blocked,
                            deferred=frozenset(deferred), expires=expires)  # fmt: skip

    def _rate_limited(self, e: SigRateLimited | None, what: str, exchange_id: str | None) -> None:
        """Respect Retry-After: no re-quote (cancel or post) until it has
        passed; the quoter re-prices afterwards, so nothing stale goes out."""
        retry_after = e.retry_after if e is not None and e.retry_after is not None else self._rules.rate_limit_pause_seconds
        until = self._clock() + timedelta(seconds=retry_after)
        self._pacer.pause_until(until)
        self._store.log("requote_paused", {"what": what, "exchange_id": exchange_id, "retry_after": retry_after,
                                           "until": until.isoformat()})  # fmt: skip


    async def place_test_order(self, order: Order) -> Order | None:
        """Go-live gate (c)/(d) only: one order through
        RiskManager.check_test_order (1 share, extreme price, every other
        limit). Returns the placed order, or None if refused."""
        if self._control.halted or self._refused(order, self._clock()):
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
