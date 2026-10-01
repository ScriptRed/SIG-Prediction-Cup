"""predcup/orders.py: the only path from a strategy to a venue. Every order
goes through risk.check() (CLAUDE.md hard rule 1); shadow mode places and
cancels nothing."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from _helpers import full_size_ramp
from predcup.control import TradingControl
from predcup.fairvalue import FairValue
from predcup.models import Order, OrderStatus
from predcup.orders import OrderRouter
from predcup.risk import RiskDecision, RiskLimits, RiskManager
from predcup.store import EventStore
from predcup.venues.base import BatchItemResult
from predcup.venues.sig import OrderStatusUnknown, SigOrderRejected
from sim.mock_exchange import MockExchange

TID = "550e8400-e29b-41d4-a716-446655440000"
NOW = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)
FV = FairValue(ok=True, value=0.5, uncertainty=0.01, as_of=NOW - timedelta(seconds=5))


class Alerts:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, m: str) -> None:
        self.messages.append(m)


def run(c):
    return asyncio.run(c)


def order(key="k1", price=0.49, action="buy", market_id="m1", exchange_id="e1") -> Order:
    return Order(exchange_id=exchange_id, market_id=market_id, tournament_id=TID, party_id="D", race_key="MA-Senate",
                 side="yes", action=action, quantity=10, price=price, idempotency_key=key,
                 expiration_date=NOW + timedelta(seconds=30))  # fmt: skip


def make(tmp_path, venue=None, shadow=False, risk=None):
    store = EventStore(tmp_path / "e.db")
    alerts = Alerts()
    venue = venue or MockExchange()
    risk = risk or RiskManager(
        limits=RiskLimits(max_bankroll_fraction_per_market=1.0, max_total_exposure_fraction=1.0,
                          max_party_exposure_fraction=1.0, max_order_size_susqies=1_000_000,
                          max_price_deviation_from_fair_value=0.05, daily_loss_stop_fraction=1.0,
                          stale_data_stop_seconds=60),  # fmt: skip
        bankroll=100_000.0, event_store=store, venue=venue, tournament_id=TID, alerter=alerts,
        size_ramp=full_size_ramp(), fusion_race_keys=frozenset(),
    )  # fmt: skip
    control = TradingControl()
    router = OrderRouter(venue=venue, risk=risk, store=store, tournament_id=TID, shadow=shadow,
                         alerter=alerts, control=control)  # fmt: skip
    return router, venue, store, alerts, control, risk


class DenyAll:
    def __init__(self):
        self.checked = 0

    def check(self, order, *, fair_value, outside_data_age_seconds):
        self.checked += 1
        return RiskDecision(False, "denied for test")


class SpyVenue(MockExchange):
    def __init__(self):
        super().__init__()
        self.batches: list[list[Order]] = []
        self.cancel_scopes: list[tuple[str | None, str | None]] = []

    async def place_batch(self, orders, batch_key):
        self.batches.append(list(orders))
        return await super().place_batch(orders, batch_key)

    async def cancel_all(self, tournament_id, exchange_id=None, market_id=None):
        self.cancel_scopes.append((exchange_id, market_id))
        return await super().cancel_all(tournament_id, exchange_id, market_id)


def upd(*orders, fv=FV):
    """{exchange_id: [(order, fv), ...]} for router.requote."""
    out: dict = {}
    for o in orders:
        out.setdefault(o.exchange_id, []).append((o, fv))
    return out


def test_risk_denial_means_nothing_reaches_the_venue(tmp_path):
    spy = SpyVenue()
    deny = DenyAll()
    router, *_ = make(tmp_path, venue=spy, risk=deny)
    res = run(router.requote(upd(order("a"), order("b", action="sell", price=0.51)), NOW))
    assert deny.checked == 2
    assert spy.batches == [] and res.placed == 0


def test_shadow_mode_places_and_cancels_nothing_but_logs(tmp_path):
    spy = SpyVenue()
    router, _, store, *_ = make(tmp_path, venue=spy, shadow=True)
    res = run(router.requote(upd(order("a")), NOW))
    assert spy.batches == [] and spy.cancel_scopes == []
    assert res.shadow and res.approved == 1 and res.placed == 0
    ev = store.all_events("shadow_quote")
    assert ev[0]["payload"]["price"] == 0.49 and ev[0]["payload"]["fair_value"] == 0.5
    assert store.all_events("shadow_cancel")[0]["payload"]["exchange_id"] == "e1"


def test_shadow_mode_still_runs_risk_and_logs_rejections(tmp_path):
    router, _, store, *_ = make(tmp_path, shadow=True)
    res = run(router.requote(upd(order("a", price=0.30)), NOW))  # 20 points from fair value 0.5
    assert res.approved == 0
    assert store.all_events("risk_rejection")[0]["payload"]["reason"].startswith("price deviates")


def test_live_cancels_only_the_requoted_market_then_places_one_batch(tmp_path):
    spy = SpyVenue()
    router, _, store, *_ = make(tmp_path, venue=spy)
    res = run(router.requote(upd(order("a"), order("b", action="sell", price=0.51)), NOW))
    assert spy.cancel_scopes == [("e1", None)]
    assert len(spy.batches) == 1 and len(spy.batches[0]) == 2
    assert res.placed == 2 and res.done == {"e1"}
    assert len(store.all_events("order")) == 2


def test_manual_orders_in_other_markets_survive_a_requote(tmp_path):
    spy = SpyVenue()
    router, *_ = make(tmp_path, venue=spy)
    manual = run(spy.place_order(order("manual", exchange_id="e9", market_id="m9")))
    run(router.requote(upd(order("a")), NOW))
    run(router.requote(upd(order("b")), NOW))
    assert manual.id in {o.id for o in run(spy.get_open_orders(TID))}
    assert all(scope == ("e1", None) for scope in spy.cancel_scopes)


def test_router_never_cancels_tournament_wide(tmp_path):
    spy = SpyVenue()
    router, *_ = make(tmp_path, venue=spy)
    run(router.requote(upd(order("a"), order("c", exchange_id="e2", market_id="m2")), NOW))
    run(router.requote({"e1": [], "e2": []}, NOW))  # pull both
    assert (None, None) not in spy.cancel_scopes
    assert sorted(spy.cancel_scopes) == [("e1", None), ("e1", None), ("e2", None), ("e2", None)]


def test_empty_update_pulls_that_market(tmp_path):
    spy = SpyVenue()
    router, *_ = make(tmp_path, venue=spy)
    run(router.requote(upd(order("a"), order("c", exchange_id="e2", market_id="m2")), NOW))
    res = run(router.requote({"e2": []}, NOW))
    assert {o.exchange_id for o in run(spy.get_open_orders(TID))} == {"e1"}
    assert res.done == {"e2"}


def test_swept_quotes_stay_counted_until_reconciliation_releases_them(tmp_path):
    # A swept quote may have filled just before the sweep; its exposure only
    # moves into positions at the next clean reconciliation.
    spy = SpyVenue()
    router, _, _, _, _, risk = make(tmp_path, venue=spy)
    run(router.requote(upd(order("a")), NOW))
    run(router.requote(upd(order("b")), NOW))
    assert len(run(spy.get_open_orders(TID))) == 1
    statuses = {o.idempotency_key: o.status for o in risk._tracked_orders()}
    assert statuses == {"a": OrderStatus.OPEN, "b": OrderStatus.OPEN}
    snapshot = router.swept_snapshot()
    assert snapshot == ["a"]
    run(router.requote(upd(order("c")), NOW))  # sweeps b after the snapshot
    router.release_swept(snapshot)
    statuses = {o.idempotency_key: o.status for o in risk._tracked_orders()}
    assert statuses == {"b": OrderStatus.OPEN, "c": OrderStatus.OPEN}  # "a" released: cancelled, so forgotten
    assert router.swept_snapshot() == ["b"]


def test_more_than_50_orders_are_split_into_batches(tmp_path):
    spy = SpyVenue()
    router, *_ = make(tmp_path, venue=spy)
    run(router.requote(upd(*[order(f"k{i}", exchange_id=f"e{i}", market_id=f"m{i}") for i in range(60)]), NOW))
    assert [len(b) for b in spy.batches] == [50, 10]


def test_market_with_orders_left_after_cancel_is_not_reposted_others_are(tmp_path):
    spy = SpyVenue()
    router, _, _, alerts, *_ = make(tmp_path, venue=spy)
    run(router.requote(upd(order("a"), order("c", exchange_id="e2", market_id="m2")), NOW))
    stuck = next(o.id for o in run(spy.get_open_orders(TID)) if o.exchange_id == "e1")
    spy.configure_cancel_all_to_silently_miss({stuck})
    res = run(router.requote(upd(order("b"), order("d", exchange_id="e2", market_id="m2")), NOW))
    assert res.done == {"e2"} and res.placed == 1
    assert [o.exchange_id for o in spy.batches[-1]] == ["e2"]
    assert any("remain" in m for m in alerts.messages)


class RejectingVenue(SpyVenue):
    async def place_batch(self, orders, batch_key):
        self.batches.append(list(orders))
        return [BatchItemResult(0, True, 201, orders[0].model_copy(update={"id": "1", "status": OrderStatus.OPEN})),
                BatchItemResult(1, False, 400, orders[1], "INSUFFICIENT_BALANCE", "no")]  # fmt: skip


def test_item_4xx_halts_that_market_via_risk(tmp_path):
    v = RejectingVenue()
    router, _, _, alerts, _, risk = make(tmp_path, venue=v)
    run(router.requote(upd(order("a"), order("b", market_id="m2", exchange_id="e2")), NOW))
    assert risk.is_market_halted("m2") and not risk.is_market_halted("m1")
    statuses = {o.idempotency_key: o.status for o in risk._tracked_orders()}
    assert statuses == {"a": OrderStatus.OPEN}  # "b" rejected: never counts, forgotten


class UnknownVenue(SpyVenue):
    async def place_batch(self, orders, batch_key):
        raise OrderStatusUnknown(batch_key)


def test_status_unknown_halts_quoting_until_reconciled(tmp_path):
    router, _, _, alerts, control, risk = make(tmp_path, venue=UnknownVenue())
    run(router.requote(upd(order("a")), NOW))
    assert router.blocked
    assert [o.status for o in risk._tracked_orders()] == [OrderStatus.PENDING]
    assert any("unknown" in m.lower() for m in alerts.messages)
    res = run(router.requote(upd(order("b")), NOW))
    assert res.placed == 0 and res.blocked
    router.clear_block("reconciled")
    assert not router.blocked


class ValidationVenue(SpyVenue):
    async def place_batch(self, orders, batch_key):
        raise SigOrderRejected(400, "VALIDATION_ERROR", "bad input")


def test_whole_batch_rejection_halts_trading(tmp_path):
    router, _, _, alerts, control, risk = make(tmp_path, venue=ValidationVenue())
    run(router.requote(upd(order("a")), NOW))
    assert control.halted and "VALIDATION_ERROR" in control.reason
    assert risk._tracked_orders() == []  # rejected: forgotten


def test_halted_control_blocks_everything(tmp_path):
    spy = SpyVenue()
    router, _, _, _, control, _ = make(tmp_path, venue=spy)
    control.halt("KILL file")
    res = run(router.requote(upd(order("a")), NOW))
    assert res.placed == 0 and spy.batches == [] and spy.cancel_scopes == []


def test_stale_fair_value_is_passed_to_risk_as_age(tmp_path):
    router, _, store, *_ = make(tmp_path, shadow=True)
    old = FairValue(ok=True, value=0.5, uncertainty=0.01, as_of=NOW - timedelta(seconds=90))
    res = run(router.requote(upd(order("a"), fv=old), NOW))
    assert res.approved == 0
    assert store.all_events("risk_rejection")[0]["payload"]["reason"] == "stale outside data"


def test_unavailable_fair_value_is_never_sent(tmp_path):
    router, *_ = make(tmp_path, shadow=True)
    with pytest.raises(ValueError):
        run(router.requote(upd(order("a"), fv=FairValue.none("stale")), NOW))


# --- audit 2026-10-01 H2: nothing may be posted, or left resting, after a halt ----------


class HaltDuringFirstBatchVenue(SpyVenue):
    def __init__(self, control):
        super().__init__()
        self.control = control

    async def place_batch(self, orders, batch_key):
        result = await super().place_batch(orders, batch_key)
        self.control.halt("kill arrived while the batch was in flight")
        return result


def test_halt_during_a_batch_stops_later_chunks_and_cancels_what_just_landed(tmp_path):
    control = TradingControl()
    venue = HaltDuringFirstBatchVenue(control)
    router, _, store, _, _, _ = make(tmp_path, venue=venue)
    router._control = control
    res = run(router.requote(upd(*[order(f"k{i}", exchange_id=f"e{i}", market_id=f"m{i}") for i in range(60)]), NOW))
    assert len(venue.batches) == 1  # second chunk never sent
    assert run(venue.get_open_orders(TID)) == []  # the 50 that landed were cancelled by id
    assert res.placed == 0
    assert store.all_events("late_orders_cancelled")[0]["payload"]["count"] == 50


def test_placement_counter_and_idle_wait(tmp_path):
    router, *_ = make(tmp_path)
    before = router.placement_count
    run(router.requote(upd(order("a")), NOW))
    assert router.placement_count == before + 1
    assert run(router.wait_idle(0.1)) is True


# --- audit 2026-10-01 M1: a 5xx batch item has an unknown outcome, not a rejection ------


class UnknownItemVenue(SpyVenue):
    async def place_batch(self, orders, batch_key):
        self.batches.append(list(orders))
        return [BatchItemResult(0, True, 201, orders[0].model_copy(update={"id": "1", "status": OrderStatus.OPEN})),
                BatchItemResult(1, False, 502, orders[1], "ORDER_STATUS_UNKNOWN", "may have gone through"),
                BatchItemResult(2, False, 429, orders[2], "RATE_LIMITED", "nothing placed")]  # fmt: skip


def test_5xx_item_stays_counted_and_suspends_quoting_until_reconciled(tmp_path):
    router, _, store, alerts, _, risk = make(tmp_path, venue=UnknownItemVenue())
    run(router.requote(upd(order("a"), order("b", exchange_id="e2", market_id="m2"),
                           order("c", exchange_id="e3", market_id="m3")), NOW))  # fmt: skip
    statuses = {o.idempotency_key: o.status for o in risk._tracked_orders()}
    assert statuses == {"a": OrderStatus.OPEN, "b": OrderStatus.PENDING}  # "c" (429) rejected: forgotten
    assert router.blocked and "unknown" in router.blocked.lower()
    assert not risk.is_market_halted("m2")  # not a 4xx rejection


# --- audit L2/L3 adopted 2026-10-01: no order without an expiry, no market orders -------


def test_order_without_expiry_is_refused_and_others_still_go(tmp_path):
    spy = SpyVenue()
    router, _, store, *_ = make(tmp_path, venue=spy)
    no_expiry = order("x", exchange_id="e2", market_id="m2").model_copy(update={"expiration_date": None})
    res = run(router.requote(upd(order("a"), no_expiry), NOW))
    sent = [o.idempotency_key for b in spy.batches for o in b]
    assert sent == ["a"] and res.placed == 1
    assert store.all_events("order_refused")[0]["payload"]["reason"] == "no expirationDate"


def test_market_order_is_refused(tmp_path):
    spy = SpyVenue()
    router, _, store, *_ = make(tmp_path, venue=spy)
    market = order("m").model_copy(update={"price": None, "expiration_date": None})
    run(router.requote(upd(market), NOW))
    assert spy.batches == []
    assert store.all_events("order_refused")[0]["payload"]["reason"] == "market order (no limit price)"


def test_refused_orders_never_reach_risk(tmp_path):
    deny = DenyAll()
    router, *_ = make(tmp_path, risk=deny)
    run(router.requote(upd(order("x").model_copy(update={"expiration_date": None})), NOW))
    assert deny.checked == 0


def test_shadow_mode_refuses_them_too(tmp_path):
    router, _, store, *_ = make(tmp_path, shadow=True)
    res = run(router.requote(upd(order("x").model_copy(update={"expiration_date": None})), NOW))
    assert res.approved == 0 and store.all_events("order_refused")


def test_test_order_without_expiry_is_refused(tmp_path):
    spy = SpyVenue()
    router, *_ = make(tmp_path, venue=spy)
    t = order("t", price=0.005).model_copy(update={"expiration_date": None, "quantity": 1})
    assert run(router.place_test_order(t)) is None and spy.batches == []


# --- 2026-10-01 launch: a timed-out batch suspends quoting like ORDER_STATUS_UNKNOWN ----


def test_timed_out_batch_keeps_orders_counted_and_blocks_until_reconciled(tmp_path):
    import httpx

    from predcup.venues.sig import SigVenue

    def handler(request):
        if request.url.path.endswith("/tournaments/cup"):
            return httpx.Response(200, json={"id": TID})
        if request.url.path.endswith("/orders/batch"):
            raise httpx.ReadTimeout("SIG slow at the open", request=request)
        if request.url.path.endswith("/orders/cancel-all"):
            return httpx.Response(200, json={"cancelled": 0, "errors": []})
        return httpx.Response(200, json={"data": [], "pagination": {"limit": 200, "hasMore": False, "nextCursor": None}})

    async def nosleep(s):
        pass

    venue = SigVenue(httpx.AsyncClient(transport=httpx.MockTransport(handler)), base_url="https://sig.test/api/v1",
                     api_key="k", tournament_slug="cup", on_rate_limited=lambda e, r: None, sleep=nosleep)  # fmt: skip
    router, _, store, alerts, _, risk = make(tmp_path, venue=venue)
    res = run(router.requote(upd(order("a")), NOW))  # must not raise
    assert router.blocked and "unknown" in router.blocked.lower()
    assert [(o.idempotency_key, o.status) for o in risk._tracked_orders()] == [("a", OrderStatus.PENDING)]
    assert res.placed == 0
    res2 = run(router.requote(upd(order("b")), NOW))  # next cycle: no new key while unresolved
    assert res2.blocked and [o.idempotency_key for o in risk._tracked_orders()] == ["a"]
