"""perf 2026-10-01: RiskManager forgets orders that can never count again.
Every order ever placed used to stay in RiskManager._orders, and every
risk.check() scanned them all (25 ms per check at 100k orders, ~1.3 s per
re-quote cycle; the bot places ~9k orders an hour at 25 markets). Orders
confirmed cancelled/expired/rejected never count toward exposure, nor do
fully-filled ones once positions are known, so dropping them must not
change any decision."""

from __future__ import annotations

import time

import pytest

from _helpers import full_size_ramp
from predcup.models import Order, OrderStatus
from predcup.risk import PositionExposure, RiskLimits, RiskManager
from predcup.store import EventStore
from sim.mock_exchange import MockExchange

TID = "550e8400-e29b-41d4-a716-446655440000"


class Alerts:
    def send(self, m):
        pass


def make(tmp_path):
    return RiskManager(
        limits=RiskLimits(max_bankroll_fraction_per_market=0.05, max_total_exposure_fraction=0.6,
                          max_party_exposure_fraction=0.25, max_order_size_susqies=1e9,
                          max_price_deviation_from_fair_value=1.0, daily_loss_stop_fraction=1.0,
                          stale_data_stop_seconds=60),  # fmt: skip
        bankroll=10_000.0, event_store=EventStore(tmp_path / "e.db"), venue=MockExchange(), tournament_id=TID,
        alerter=Alerts(), size_ramp=full_size_ramp(), fusion_race_keys=frozenset(),
    )  # fmt: skip


def o(key, qty=100, price=0.5, market="m1"):
    return Order(exchange_id="e-" + market, market_id=market, tournament_id=TID, party_id="R", race_key="MI-Senate",
                 side="yes", action="buy", quantity=qty, price=price, idempotency_key=key)  # fmt: skip


@pytest.mark.parametrize("status", [OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED])
def test_freed_orders_are_forgotten(tmp_path, status):
    risk = make(tmp_path)
    risk.record_order(o("a"))
    risk.confirm_order_state("a", status)
    assert risk._tracked_orders() == []


def test_open_and_pending_orders_are_kept(tmp_path):
    risk = make(tmp_path)
    risk.record_order(o("a"))
    risk.record_order(o("b"))
    risk.confirm_order_state("b", OrderStatus.OPEN)
    assert {x.idempotency_key for x in risk._tracked_orders()} == {"a", "b"}


def test_filled_orders_kept_until_positions_are_known_then_forgotten(tmp_path):
    risk = make(tmp_path)
    risk.record_order(o("f"))
    risk.confirm_order_state("f", OrderStatus.FILLED)
    assert [x.idempotency_key for x in risk._tracked_orders()] == ["f"]  # still the only record of that exposure
    risk.update_positions([PositionExposure(market_id="m1", party_id="R", race_key="MI-Senate", quantity=100, price=0.5)])
    assert risk._tracked_orders() == []
    risk.record_order(o("g"))
    risk.confirm_order_state("g", OrderStatus.FILLED)  # positions already known: dropped at once
    assert risk._tracked_orders() == []


def test_decisions_unchanged_by_forgotten_orders(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with_dead = make(tmp_path / "a")
    without = make(tmp_path / "b")
    for r in (with_dead, without):
        r.record_order(o("live", qty=800))  # 400 of the 500 per-market cap
    for i in range(1000):
        with_dead.record_order(o(f"dead{i}"))
        with_dead.confirm_order_state(f"dead{i}", OrderStatus.CANCELLED)
    for qty in (100, 200, 201, 300):
        a = with_dead.check(o("new", qty=qty), fair_value=0.5, outside_data_age_seconds=0)
        b = without.check(o("new", qty=qty), fair_value=0.5, outside_data_age_seconds=0)
        assert (a.approved, a.reason) == (b.approved, b.reason)


def test_check_time_does_not_grow_with_order_history(tmp_path):
    risk = make(tmp_path)
    for i in range(100_000):
        risk.record_order(o(str(i)))
        risk.confirm_order_state(str(i), OrderStatus.CANCELLED)
    start = time.perf_counter()
    for _ in range(50):
        risk.check(o("x"), fair_value=0.5, outside_data_age_seconds=0)
    assert (time.perf_counter() - start) / 50 < 0.002  # was ~25 ms with 100k dead orders tracked
