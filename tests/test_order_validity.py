"""Never send an invalid order (2026-10-13 live run).

At 16:07:59 the quoter built orders expiring at 16:08:29 (cycle time + 30 s).
Three of its per-market cancel-alls hit 429 with Retry-After 60, the
adapter waited it out, and the batch went out at 16:09:00 with every
expirationDate already in the past: 400 VALIDATION_ERROR for the whole
batch, which halted trading. Now expirationDate is stamped at send time
(after the cancels), and nothing reaches the venue that is expired or too
close to expiry, off-tick, outside [0.005, 0.995], or sized below 1 share.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from _helpers import full_size_ramp
from predcup.control import TradingControl
from predcup.fairvalue import FairValue
from predcup.models import Order, OrderStatus
from predcup.orders import OrderRouter, SendRules, invalid_reason, load_send_rules
from predcup.risk import RiskLimits, RiskManager
from predcup.store import EventStore
from predcup.venues.sig import OrderStatusUnknown, SigVenue
from sim.mock_exchange import MockExchange

TID = "550e8400-e29b-41d4-a716-446655440000"
T0 = datetime(2026, 10, 13, 16, 7, 59, 673000, tzinfo=timezone.utc)
RULES = SendRules(quote_expiration_seconds=30, min_expiry_margin_seconds=10)


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class Alerts:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, m: str) -> None:
        self.messages.append(m)


def order(key="k1", price=0.49, action="buy", exchange_id="e1", market_id="m1", expires=T0 + timedelta(seconds=30), qty=10):
    return Order(exchange_id=exchange_id, market_id=market_id, tournament_id=TID, party_id="D", race_key="MA-Senate",
                 side="yes", action=action, quantity=qty, price=price, idempotency_key=key,
                 expiration_date=expires)  # fmt: skip


# --- invalid_reason: the pure check ---------------------------------------------------


def test_a_good_order_is_valid():
    assert invalid_reason(order(), T0, 10) == ""


@pytest.mark.parametrize("expires,why", [
    (None, "no expirationDate"),
    (T0 - timedelta(seconds=31), "expired"),
    (T0 + timedelta(seconds=9), "expires in 9.0s"),
])  # fmt: skip
def test_expired_missing_or_too_close_expiry_is_invalid(expires, why):
    o = order().model_copy(update={"expiration_date": expires})
    assert why in invalid_reason(o, T0, 10)


@pytest.mark.parametrize("price", [0.4925, 0.0, 1.0, 0.999, 0.001, -0.5, None])
def test_off_tick_out_of_range_or_market_prices_are_invalid(price):
    o = order().model_copy(update={"price": price})  # model_copy skips the model's validators
    assert invalid_reason(o, T0, 10)


@pytest.mark.parametrize("qty", [0, -3, 2.5])
def test_quantity_below_one_share_or_fractional_is_invalid(qty):
    # e.g. a launch-ramp size: int(4 * 0.10) == 0
    o = order().model_copy(update={"quantity": qty})
    assert "quantity" in invalid_reason(o, T0, 10)


def test_send_rules_need_expiry_longer_than_the_margin():
    s = {"quoter": {"quote_expiration_seconds": 30}, "orders": {"min_expiry_margin_seconds": 10}}
    assert load_send_rules(s) == RULES
    with pytest.raises(ValueError):
        load_send_rules({"quoter": {"quote_expiration_seconds": 10}, "orders": {"min_expiry_margin_seconds": 10}})


# --- the router -----------------------------------------------------------------------


class SlowCancelVenue(MockExchange):
    """cancel_all takes `cancel_seconds` on the clock (the 60 s Retry-After)."""

    def __init__(self, clock: Clock, cancel_seconds: float = 0.0):
        super().__init__()
        self.clock = clock
        self.cancel_seconds = cancel_seconds
        self.sent: list[tuple[datetime, list[Order]]] = []

    async def cancel_all(self, tournament_id, exchange_id=None, market_id=None):
        self.clock.advance(self.cancel_seconds)
        return await super().cancel_all(tournament_id, exchange_id, market_id)

    async def place_batch(self, orders, batch_key):
        self.sent.append((self.clock(), list(orders)))
        return await super().place_batch(orders, batch_key)


def make_router(tmp_path, venue, clock):
    store = EventStore(tmp_path / "e.db")
    alerts = Alerts()
    risk = RiskManager(
        limits=RiskLimits(max_bankroll_fraction_per_market=1.0, max_total_exposure_fraction=1.0,
                          max_party_exposure_fraction=1.0, max_order_size_susqies=1_000_000,
                          max_price_deviation_from_fair_value=0.05, daily_loss_stop_fraction=1.0,
                          stale_data_stop_seconds=600),  # fmt: skip
        bankroll=100_000.0, event_store=store, venue=venue, tournament_id=TID, alerter=alerts,
        size_ramp=full_size_ramp(), fusion_race_keys=frozenset(), now=clock,
    )  # fmt: skip
    router = OrderRouter(venue=venue, risk=risk, store=store, tournament_id=TID, shadow=False, alerter=alerts,
                         control=TradingControl(), send_rules=RULES, clock=clock)  # fmt: skip
    return router, store, risk


def fv(clock):
    return FairValue(ok=True, value=0.5, uncertainty=0.01, as_of=clock() - timedelta(seconds=2))


def test_expiry_is_stamped_at_send_time_after_a_long_cancel(tmp_path):
    clock = Clock()
    venue = SlowCancelVenue(clock, cancel_seconds=61)  # the incident: Retry-After 60 inside the cancel
    router, store, _ = make_router(tmp_path, venue, clock)
    built = order(expires=T0 + timedelta(seconds=30))  # what the quoter built at the cycle start
    run(router.requote({"e1": [(built, fv(clock))]}, T0))
    [(sent_at, [sent])] = venue.sent
    assert sent_at == T0 + timedelta(seconds=61)
    assert sent.expiration_date == sent_at + timedelta(seconds=30)  # not the stale T0 + 30 s
    [ev] = store.all_events("order")
    assert ev["payload"]["expiration_date"] == sent.expiration_date.isoformat()


def test_invalid_orders_never_reach_the_venue_and_the_rest_still_go(tmp_path):
    clock = Clock()
    venue = SlowCancelVenue(clock)
    router, store, risk = make_router(tmp_path, venue, clock)
    good = order("good")
    zero = order("zero", exchange_id="e2", market_id="m2").model_copy(update={"quantity": 0})
    off_tick = order("off", exchange_id="e3", market_id="m3").model_copy(update={"price": 0.4925})
    upd = {"e1": [(good, fv(clock))], "e2": [(zero, fv(clock))], "e3": [(off_tick, fv(clock))]}
    run(router.requote(upd, T0))
    assert [o.idempotency_key for _, batch in venue.sent for o in batch] == ["good"]
    refused = {e["payload"]["key"] for e in store.all_events("order_refused")}
    assert refused == {"zero", "off"}
    assert [o.idempotency_key for o in risk._tracked_orders()] == ["good"]


def test_final_gate_refuses_an_order_that_would_expire_within_the_margin(tmp_path):
    # Belt and braces: if the stamp were ever shorter than the margin, the
    # gate (checked against the clock at send time) still refuses it.
    clock = Clock()
    venue = SlowCancelVenue(clock)
    router, store, _ = make_router(tmp_path, venue, clock)
    router._rules = SendRules(quote_expiration_seconds=5, min_expiry_margin_seconds=10)
    run(router.requote({"e1": [(order(), fv(clock))]}, T0))
    assert venue.sent == []
    assert "expires in" in store.all_events("order_refused")[0]["payload"]["reason"]


def test_test_order_too_close_to_expiry_is_refused(tmp_path):
    clock = Clock()
    venue = SlowCancelVenue(clock)
    router, store, _ = make_router(tmp_path, venue, clock)
    assert run(router.place_test_order(order(price=0.005, qty=1, expires=T0 + timedelta(seconds=3)))) is None
    assert venue.sent == []


# --- the SIG adapter: last line of defence -----------------------------------------------


def make_sig(handler, clock, margin=10.0):
    async def fake_sleep(s):
        clock.advance(s)

    reads = {"n": 0}

    def wrapped(request):
        if request.url.path.endswith("/tournaments/cup") and request.method == "GET":
            return httpx.Response(200, json={"id": TID})
        reads["n"] += 1
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    venue = SigVenue(client, base_url="https://sig.test/api/v1", api_key="k", tournament_slug="cup",
                     on_rate_limited=lambda e, ra: None, sleep=fake_sleep, max_retries=4,
                     clock=clock, min_expiry_margin_seconds=margin)  # fmt: skip
    return venue, reads


def test_adapter_refuses_to_send_an_expired_order():
    clock = Clock(T0 + timedelta(seconds=61))
    venue, reads = make_sig(lambda r: httpx.Response(200, json={"results": []}), clock)
    with pytest.raises(ValueError, match="expired"):
        run(venue.place_batch([order(expires=T0 + timedelta(seconds=30))], "b1"))
    assert reads["n"] == 0


def test_adapter_refuses_off_tick_or_zero_quantity():
    clock = Clock()
    venue, reads = make_sig(lambda r: httpx.Response(200, json={"results": []}), clock)
    with pytest.raises(ValueError):
        run(venue.place_batch([order().model_copy(update={"quantity": 0})], "b1"))
    with pytest.raises(ValueError):
        run(venue.place_batch([order().model_copy(update={"price": 0.4925})], "b2"))
    assert reads["n"] == 0


def test_adapter_does_not_resend_a_payload_whose_expiry_is_now_too_close():
    # A 429 with Retry-After 60 on the batch itself: the identical payload
    # (same key) would go out with a stale expirationDate. Stop, outcome
    # unknown, let reconciliation sort it out; never send it.
    clock = Clock()
    calls = []

    def handler(request):
        calls.append(clock())
        return httpx.Response(429, json={"error": {"code": "RATE_LIMITED", "message": "slow down"}},
                              headers={"Retry-After": "60"})  # fmt: skip

    venue, _ = make_sig(handler, clock)
    with pytest.raises(OrderStatusUnknown):
        run(venue.place_batch([order(expires=T0 + timedelta(seconds=30))], "b3"))
    assert calls == [T0]  # sent once, never re-sent after the wait
