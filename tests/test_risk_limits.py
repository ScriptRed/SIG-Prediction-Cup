import pytest

from predcup.models import Order
from predcup.risk import RiskLimits, RiskManager, load_risk_limits
from predcup.store import EventStore
from sim.mock_exchange import MockExchange

TOURNAMENT_ID = "550e8400-e29b-41d4-a716-446655440000"


class FakeAlerter:
    def __init__(self):
        self.messages = []

    def send(self, message: str) -> None:
        self.messages.append(message)


def make_order(**overrides):
    fields = dict(
        exchange_id="36",
        market_id="market-a",
        tournament_id=TOURNAMENT_ID,
        side="yes",
        action="buy",
        quantity=100,
        price=0.5,
        idempotency_key="order-1",
    )
    fields.update(overrides)
    return Order(**fields)


@pytest.fixture
def limits():
    return RiskLimits(
        max_bankroll_fraction_per_market=0.5,
        max_total_exposure_fraction=0.9,
        max_party_exposure_fraction=1.0,  # not the limit under test in this file
        max_order_size_susqies=1_000_000,  # effectively unlimited unless a test overrides
        max_price_deviation_from_fair_value=0.03,
        daily_loss_stop_fraction=0.08,
        stale_data_stop_seconds=60,
    )


@pytest.fixture
def manager(limits, tmp_path):
    return RiskManager(
        limits=limits,
        bankroll=1000.0,
        event_store=EventStore(tmp_path / "events.db"),
        venue=MockExchange(),
        tournament_id=TOURNAMENT_ID,
        alerter=FakeAlerter(),
    )


def test_order_at_market_cap_is_approved(manager):
    # bankroll 1000, cap fraction 0.5 -> 500 notional cap for one market
    order = make_order(quantity=1000, price=0.5)  # notional 500
    decision = manager.check(order, fair_value=0.5, outside_data_age_seconds=0)
    assert decision.approved


def test_order_over_market_cap_is_rejected(manager):
    order = make_order(quantity=1002, price=0.5)  # notional 501
    decision = manager.check(order, fair_value=0.5, outside_data_age_seconds=0)
    assert not decision.approved
    assert "market" in decision.reason.lower()


def test_total_exposure_cap_spans_markets(limits, tmp_path):
    manager = RiskManager(
        limits=limits,
        bankroll=1000.0,
        event_store=EventStore(tmp_path / "events.db"),
        venue=MockExchange(),
        tournament_id=TOURNAMENT_ID,
        alerter=FakeAlerter(),
    )
    # Two existing tracked orders in two different markets, each well under
    # the 500 per-market cap, but together near the 900 total cap.
    manager.record_order(make_order(market_id="a", quantity=800, price=0.5, idempotency_key="o1"))
    manager.record_order(make_order(market_id="b", quantity=800, price=0.5, idempotency_key="o2"))
    # tracked total notional so far: 400 + 400 = 800

    fits = make_order(market_id="c", quantity=200, price=0.5, idempotency_key="o3")  # +100 -> 900
    decision = manager.check(fits, fair_value=0.5, outside_data_age_seconds=0)
    assert decision.approved

    manager.record_order(fits)
    too_much = make_order(market_id="d", quantity=20, price=0.5, idempotency_key="o4")  # +10 -> 910
    decision = manager.check(too_much, fair_value=0.5, outside_data_age_seconds=0)
    assert not decision.approved
    assert "total" in decision.reason.lower()


def test_max_order_size_cap(tmp_path):
    limits = RiskLimits(
        max_bankroll_fraction_per_market=1.0,
        max_total_exposure_fraction=1.0,
        max_party_exposure_fraction=1.0,
        max_order_size_susqies=200,
        max_price_deviation_from_fair_value=0.5,
        daily_loss_stop_fraction=0.5,
        stale_data_stop_seconds=60,
    )
    manager = RiskManager(
        limits=limits,
        bankroll=100_000.0,
        event_store=EventStore(tmp_path / "events.db"),
        venue=MockExchange(),
        tournament_id=TOURNAMENT_ID,
        alerter=FakeAlerter(),
    )
    ok = make_order(quantity=400, price=0.5)  # notional 200
    assert manager.check(ok, fair_value=0.5, outside_data_age_seconds=0).approved

    too_big = make_order(quantity=402, price=0.5)  # notional 201
    decision = manager.check(too_big, fair_value=0.5, outside_data_age_seconds=0)
    assert not decision.approved
    assert "order size" in decision.reason.lower()


def test_price_deviation_from_fair_value(manager):
    within = make_order(price=0.52)
    assert manager.check(within, fair_value=0.5, outside_data_age_seconds=0).approved

    outside = make_order(price=0.54)
    decision = manager.check(outside, fair_value=0.5, outside_data_age_seconds=0)
    assert not decision.approved
    assert "fair value" in decision.reason.lower() or "deviation" in decision.reason.lower()


def test_market_order_skips_price_deviation_check(manager):
    order = make_order(price=None, quantity=100)
    decision = manager.check(order, fair_value=0.5, outside_data_age_seconds=0)
    assert decision.approved


def test_daily_loss_stop_blocks_all_new_orders(manager):
    manager.update_daily_pnl(-79)  # -7.9% of 1000, just under the 8% stop
    order = make_order(quantity=10, price=0.5)
    assert manager.check(order, fair_value=0.5, outside_data_age_seconds=0).approved

    manager.update_daily_pnl(-80)  # exactly -8%
    decision = manager.check(order, fair_value=0.5, outside_data_age_seconds=0)
    assert not decision.approved
    assert "daily loss" in decision.reason.lower()


def test_stale_outside_data_blocks_order(manager):
    fresh = make_order()
    assert manager.check(fresh, fair_value=0.5, outside_data_age_seconds=59).approved

    stale = make_order()
    decision = manager.check(stale, fair_value=0.5, outside_data_age_seconds=61)
    assert not decision.approved
    assert "stale" in decision.reason.lower()


def test_rejections_are_logged_to_events(manager, tmp_path):
    order = make_order(quantity=1002, price=0.5)  # exceeds market cap
    manager.check(order, fair_value=0.5, outside_data_age_seconds=0)

    events = manager._event_store.all_events(event_type="risk_rejection")
    assert len(events) == 1
    assert events[0]["payload"]["market_id"] == "market-a"


def test_load_risk_limits_from_config_dict():
    config = {
        "risk": {
            "max_bankroll_fraction_per_market": 0.05,
            "max_total_exposure_fraction": 0.6,
            "max_party_exposure_fraction": 0.4,
            "max_order_size_susqies": 500,
            "max_price_deviation_from_fair_value": 0.03,
            "daily_loss_stop_fraction": 0.08,
            "stale_data_stop_seconds": 60,
        }
    }
    limits = load_risk_limits(config)
    assert limits.max_bankroll_fraction_per_market == 0.05
    assert limits.max_total_exposure_fraction == 0.6


def test_load_risk_limits_fails_loudly_on_missing_key():
    config = {"risk": {"max_bankroll_fraction_per_market": 0.05}}
    with pytest.raises(KeyError):
        load_risk_limits(config)
