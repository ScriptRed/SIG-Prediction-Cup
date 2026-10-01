"""perf 2026-10-01: RiskManager forgets orders that can never count again.
Every order ever placed used to stay in RiskManager._orders, and every
risk.check() scanned them all (25 ms per check at 100k orders, ~1.3 s per
re-quote cycle; the bot places ~9k orders an hour at 25 markets). Orders
confirmed cancelled/expired/rejected never count toward exposure, nor do
fully-filled ones once positions are known, so dropping them must not
change any decision."""

from __future__ import annotations

import time
from datetime import datetime, timezone

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


def test_filled_orders_kept_until_a_later_snapshot_covers_them(tmp_path):
    risk = make(tmp_path)
    risk.record_order(o("f"))
    risk.confirm_order_state("f", OrderStatus.FILLED)
    assert [x.idempotency_key for x in risk._tracked_orders()] == ["f"]  # still the only record of that exposure
    risk.update_positions([PositionExposure(market_id="m1", party_id="R", race_key="MI-Senate", quantity=100,
                                            price=0.5)], as_of=datetime.now(timezone.utc))  # fmt: skip
    assert risk._tracked_orders() == []
    risk.record_order(o("g"))
    risk.confirm_order_state("g", OrderStatus.FILLED)  # filled after that snapshot: keeps counting
    assert [x.idempotency_key for x in risk._tracked_orders()] == ["g"]
    risk.update_positions([PositionExposure(market_id="m1", party_id="R", race_key="MI-Senate", quantity=200,
                                            price=0.5)], as_of=datetime.now(timezone.utc))  # fmt: skip
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


# --- before merging perf-loop (2026-10-01): pruning must never undercount -------------

import asyncio  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

T0 = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)


def make_clocked(tmp_path, clock):
    return RiskManager(
        limits=RiskLimits(max_bankroll_fraction_per_market=0.05, max_total_exposure_fraction=0.6,
                          max_party_exposure_fraction=0.25, max_order_size_susqies=1e9,
                          max_price_deviation_from_fair_value=1.0, daily_loss_stop_fraction=1.0,
                          stale_data_stop_seconds=60),  # fmt: skip
        bankroll=10_000.0, event_store=EventStore(tmp_path / "e.db"), venue=MockExchange(), tournament_id=TID,
        alerter=Alerts(), size_ramp=full_size_ramp(), fusion_race_keys=frozenset(), now=lambda: clock["t"],
    )  # fmt: skip


def pos(qty=800, price=0.5):
    return PositionExposure(market_id="m1", party_id="R", race_key="MI-Senate", quantity=qty, price=price)


# (a) unknown outcome (M1): never dropped while unresolved ----------------------------


def test_unknown_outcome_order_is_never_dropped_by_pruning(tmp_path):
    clock = {"t": T0}
    risk = make_clocked(tmp_path, clock)
    risk.record_order(o("unknown", qty=800))  # PENDING: a 5xx batch item, outcome unknown
    for i in range(5):  # reconciliations, other orders coming and going
        clock["t"] = T0 + timedelta(minutes=i + 1)
        risk.update_positions([], as_of=clock["t"])
        risk.record_order(o(f"other{i}", market="m2"))
        risk.confirm_order_state(f"other{i}", OrderStatus.CANCELLED)
    assert [x.idempotency_key for x in risk._tracked_orders()] == ["unknown"]
    d = risk.check(o("new", qty=300), fair_value=0.5, outside_data_age_seconds=0)  # 400 + 150 > 500
    assert not d.approved and "per-market" in d.reason


def test_unknown_outcome_item_survives_router_cycles_and_reconciliation(tmp_path):
    from test_order_router import NOW, UnknownItemVenue, make, order, upd

    router, _, _, _, _, risk = make(tmp_path, venue=UnknownItemVenue())
    run = asyncio.run
    run(router.requote(upd(order("a"), order("b", exchange_id="e2", market_id="m2"),
                           order("c", exchange_id="e3", market_id="m3")), NOW))  # fmt: skip
    risk.update_positions([], as_of=NOW + timedelta(seconds=60))  # a clean reconciliation
    router.release_swept(router.swept_snapshot())
    router.clear_block("clean reconciliation")
    statuses = {x.idempotency_key: x.status for x in risk._tracked_orders()}
    assert statuses.get("b") == OrderStatus.PENDING  # the 5xx item: still counted


# (b) a filled order is dropped only once a positions snapshot includes it ------------


def test_fill_after_the_last_snapshot_keeps_counting(tmp_path):
    clock = {"t": T0}
    risk = make_clocked(tmp_path, clock)
    risk.update_positions([], as_of=T0)  # positions known, flat
    clock["t"] = T0 + timedelta(seconds=10)
    risk.record_order(o("f", qty=800))
    risk.confirm_order_state("f", OrderStatus.FILLED)  # filled after the snapshot
    d = risk.check(o("new", qty=300), fair_value=0.5, outside_data_age_seconds=0)
    assert not d.approved and "per-market" in d.reason  # 400 filled + 150 > 500: not undercounted
    assert [x.idempotency_key for x in risk._tracked_orders()] == ["f"]


def test_snapshot_taken_before_the_fill_does_not_drop_it(tmp_path):
    clock = {"t": T0 + timedelta(seconds=30)}
    risk = make_clocked(tmp_path, clock)
    risk.record_order(o("f", qty=800))
    risk.confirm_order_state("f", OrderStatus.FILLED)  # at T0+30
    risk.update_positions([], as_of=T0 + timedelta(seconds=20))  # read started before the fill
    assert [x.idempotency_key for x in risk._tracked_orders()] == ["f"]
    assert not risk.check(o("new", qty=300), fair_value=0.5, outside_data_age_seconds=0).approved


def test_snapshot_after_the_fill_takes_over_and_the_order_is_dropped(tmp_path):
    clock = {"t": T0 + timedelta(seconds=30)}
    risk = make_clocked(tmp_path, clock)
    risk.record_order(o("f", qty=800))
    risk.confirm_order_state("f", OrderStatus.FILLED)
    risk.update_positions([pos(qty=800)], as_of=T0 + timedelta(seconds=60))  # includes the fill
    assert risk._tracked_orders() == []
    assert not risk.check(o("new", qty=300), fair_value=0.5, outside_data_age_seconds=0).approved  # 400 + 150
    assert risk.check(o("new2", qty=180), fair_value=0.5, outside_data_age_seconds=0).approved  # 400 + 90


def test_reconciler_snapshot_time_is_taken_before_the_positions_read(tmp_path):
    from predcup.control import TradingControl
    from predcup.reconcile import Reconciler

    clock = {"t": T0}
    risk = make_clocked(tmp_path, clock)

    class Venue(MockExchange):
        async def get_positions(self, tournament_id):
            # The read is in flight; an order is confirmed filled meanwhile.
            clock["t"] = T0 + timedelta(seconds=5)
            risk.record_order(o("late", qty=800))
            risk.confirm_order_state("late", OrderStatus.FILLED)
            return []

    class Router:
        def swept_snapshot(self):
            return []

        def release_swept(self, keys):
            pass

        def clear_block(self, reason):
            pass

    rec = Reconciler(venue=Venue(), store=EventStore(tmp_path / "r.db"), risk=risk, router=Router(),
                     control=TradingControl(), alerter=Alerts(), tournament_id=TID, market_meta={}, shadow=True,
                     clock=lambda: clock["t"])  # fmt: skip
    asyncio.run(rec.run_once())
    # The snapshot was taken at T0, before the fill at T0+5: the order must still count.
    assert [x.idempotency_key for x in risk._tracked_orders()] == ["late"]


# (c) H3 short-YES exposure unchanged by pruning ---------------------------------------


def test_short_yes_exposure_unchanged_with_dead_orders_around(tmp_path):
    def sell(key, qty, price=0.05):
        return Order(exchange_id="e-m1", market_id="m1", tournament_id=TID, party_id="R", race_key="MI-Senate",
                     side="yes", action="sell", quantity=qty, price=price, idempotency_key=key)  # fmt: skip

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with_dead, without = make(tmp_path / "a"), make(tmp_path / "b")
    for r in (with_dead, without):
        r.record_order(sell("live", 400))  # 400 x 0.95 = 380 of the 500 cap
    for i in range(1000):
        with_dead.record_order(sell(f"dead{i}", 100))
        with_dead.confirm_order_state(f"dead{i}", OrderStatus.REJECTED)
    for qty in (100, 126, 127, 200):  # 126 x 0.95 = 119.7 fits; 127 x 0.95 = 120.65 doesn't
        a = with_dead.check(sell("new", qty), fair_value=0.05, outside_data_age_seconds=0)
        b = without.check(sell("new", qty), fair_value=0.05, outside_data_age_seconds=0)
        assert (a.approved, a.reason) == (b.approved, b.reason)
        assert a.approved == (qty <= 126)
