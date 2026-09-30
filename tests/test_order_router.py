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
                 side="yes", action=action, quantity=10, price=price, idempotency_key=key)  # fmt: skip


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
        self.cancel_alls = 0

    async def place_batch(self, orders, batch_key):
        self.batches.append(list(orders))
        return await super().place_batch(orders, batch_key)

    async def cancel_all(self, tournament_id, exchange_id=None, market_id=None):
        self.cancel_alls += 1
        return await super().cancel_all(tournament_id, exchange_id, market_id)


def test_risk_denial_means_nothing_reaches_the_venue(tmp_path):
    spy = SpyVenue()
    deny = DenyAll()
    router, *_ = make(tmp_path, venue=spy, risk=deny)
    res = run(router.replace_all([(order("a"), FV), (order("b", action="sell", price=0.51), FV)], NOW))
    assert deny.checked == 2
    assert spy.batches == [] and res.placed == 0


def test_shadow_mode_places_and_cancels_nothing_but_logs(tmp_path):
    spy = SpyVenue()
    router, _, store, *_ = make(tmp_path, venue=spy, shadow=True)
    res = run(router.replace_all([(order("a"), FV)], NOW))
    assert spy.batches == [] and spy.cancel_alls == 0
    assert res.shadow and res.approved == 1 and res.placed == 0
    ev = store.all_events("shadow_quote")
    assert ev[0]["payload"]["price"] == 0.49 and ev[0]["payload"]["fair_value"] == 0.5


def test_shadow_mode_still_runs_risk_and_logs_rejections(tmp_path):
    router, _, store, *_ = make(tmp_path, shadow=True)
    far = order("a", price=0.30)  # 20 points from fair value 0.5
    res = run(router.replace_all([(far, FV)], NOW))
    assert res.approved == 0
    assert store.all_events("risk_rejection")[0]["payload"]["reason"].startswith("price deviates")


def test_live_cancels_then_places_one_batch(tmp_path):
    spy = SpyVenue()
    router, _, store, *_ = make(tmp_path, venue=spy)
    res = run(router.replace_all([(order("a"), FV), (order("b", action="sell", price=0.51), FV)], NOW))
    assert spy.cancel_alls == 1 and len(spy.batches) == 1 and len(spy.batches[0]) == 2
    assert res.placed == 2
    assert len(run(spy.get_open_orders(TID))) == 2
    assert len(store.all_events("order")) == 2


def test_live_requote_cancels_previous_quotes_and_frees_their_exposure(tmp_path):
    spy = SpyVenue()
    router, _, _, _, _, risk = make(tmp_path, venue=spy)
    run(router.replace_all([(order("a"), FV)], NOW))
    run(router.replace_all([(order("b"), FV)], NOW))
    assert len(run(spy.get_open_orders(TID))) == 1
    statuses = {o.idempotency_key: o.status for o in risk._tracked_orders()}
    assert statuses == {"a": OrderStatus.CANCELLED, "b": OrderStatus.OPEN}


def test_more_than_50_orders_are_split_into_batches(tmp_path):
    spy = SpyVenue()
    router, *_ = make(tmp_path, venue=spy)
    orders = [(order(f"k{i}", exchange_id=f"e{i}", market_id=f"m{i}"), FV) for i in range(60)]
    run(router.replace_all(orders, NOW))
    assert [len(b) for b in spy.batches] == [50, 10]


def test_orders_left_open_after_cancel_all_block_reposting(tmp_path):
    spy = SpyVenue()
    router, _, _, alerts, *_ = make(tmp_path, venue=spy)
    run(router.replace_all([(order("a"), FV)], NOW))
    live_id = run(spy.get_open_orders(TID))[0].id
    spy.configure_cancel_all_to_silently_miss({live_id})

    # MockExchange reports remaining=0 even when it misses; the router must
    # still confirm via get_open_orders before re-posting.
    res = run(router.replace_all([(order("b"), FV)], NOW))
    assert res.placed == 0 and "remain" in res.blocked
    assert len(spy.batches) == 1
    assert any("remain" in m for m in alerts.messages)


class RejectingVenue(SpyVenue):
    async def place_batch(self, orders, batch_key):
        self.batches.append(list(orders))
        return [BatchItemResult(0, True, 201, orders[0].model_copy(update={"id": "1", "status": OrderStatus.OPEN})),
                BatchItemResult(1, False, 400, orders[1], "INSUFFICIENT_BALANCE", "no")]  # fmt: skip


def test_item_4xx_halts_that_market_via_risk(tmp_path):
    v = RejectingVenue()
    router, _, _, alerts, _, risk = make(tmp_path, venue=v)
    run(router.replace_all([(order("a"), FV), (order("b", market_id="m2", exchange_id="e2"), FV)], NOW))
    assert risk.is_market_halted("m2") and not risk.is_market_halted("m1")
    statuses = {o.idempotency_key: o.status for o in risk._tracked_orders()}
    assert statuses == {"a": OrderStatus.OPEN, "b": OrderStatus.REJECTED}


class UnknownVenue(SpyVenue):
    async def place_batch(self, orders, batch_key):
        raise OrderStatusUnknown(batch_key)


def test_status_unknown_halts_quoting_until_reconciled(tmp_path):
    router, _, _, alerts, control, risk = make(tmp_path, venue=UnknownVenue())
    run(router.replace_all([(order("a"), FV)], NOW))
    assert router.blocked
    # Exposure stays counted (PENDING) until reconciliation says otherwise.
    assert [o.status for o in risk._tracked_orders()] == [OrderStatus.PENDING]
    assert any("unknown" in m.lower() for m in alerts.messages)
    res = run(router.replace_all([(order("b"), FV)], NOW))
    assert res.placed == 0 and res.blocked
    router.clear_block("reconciled")
    assert not router.blocked


class ValidationVenue(SpyVenue):
    async def place_batch(self, orders, batch_key):
        raise SigOrderRejected(400, "VALIDATION_ERROR", "bad input")


def test_whole_batch_rejection_halts_trading(tmp_path):
    router, _, _, alerts, control, risk = make(tmp_path, venue=ValidationVenue())
    run(router.replace_all([(order("a"), FV)], NOW))
    assert control.halted and "VALIDATION_ERROR" in control.reason
    assert [o.status for o in risk._tracked_orders()] == [OrderStatus.REJECTED]


def test_halted_control_blocks_everything(tmp_path):
    spy = SpyVenue()
    router, _, _, _, control, _ = make(tmp_path, venue=spy)
    control.halt("KILL file")
    res = run(router.replace_all([(order("a"), FV)], NOW))
    assert res.placed == 0 and spy.batches == [] and spy.cancel_alls == 0


def test_stale_fair_value_is_passed_to_risk_as_age(tmp_path):
    router, _, store, *_ = make(tmp_path, shadow=True)
    old = FairValue(ok=True, value=0.5, uncertainty=0.01, as_of=NOW - timedelta(seconds=90))
    res = run(router.replace_all([(order("a"), old)], NOW))
    assert res.approved == 0
    assert store.all_events("risk_rejection")[0]["payload"]["reason"] == "stale outside data"


def test_unavailable_fair_value_is_never_sent(tmp_path):
    router, *_ = make(tmp_path, shadow=True)
    with pytest.raises(ValueError):
        run(router.replace_all([(order("a"), FairValue.none("stale"))], NOW))
