"""Per-market caps don't cover correlated polling error: several different
race markets can all be a bet on the same party, and a bad polling miss
moves them together. This is the net party-exposure limit — total exposure
to one party across all races, independent of which market each order sits
in.
"""

import pytest

from predcup.models import Order
from predcup.risk import RiskLimits, RiskManager
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
        market_id="senate-race-a",
        tournament_id=TOURNAMENT_ID,
        party_id="GOP",
        side="yes",
        action="buy",
        quantity=100,
        price=0.5,
        idempotency_key="order-1",
    )
    fields.update(overrides)
    return Order(**fields)


@pytest.fixture
def manager(tmp_path):
    limits = RiskLimits(
        max_bankroll_fraction_per_market=1.0,  # not the limit under test
        max_total_exposure_fraction=1.0,  # not the limit under test
        max_party_exposure_fraction=0.5,  # bankroll 1000 -> cap 500
        max_order_size_susqies=1_000_000,
        max_price_deviation_from_fair_value=1.0,
        daily_loss_stop_fraction=1.0,
        stale_data_stop_seconds=60,
    )
    return RiskManager(
        limits=limits,
        bankroll=1000.0,
        event_store=EventStore(tmp_path / "events.db"),
        venue=MockExchange(),
        tournament_id=TOURNAMENT_ID,
        alerter=FakeAlerter(),
    )


def test_party_exposure_aggregates_across_different_markets(manager):
    # Same party, three different race markets: 200 + 200 = 400 tracked.
    manager.record_order(
        make_order(market_id="senate-race-a", quantity=400, price=0.5, idempotency_key="o1")
    )
    manager.record_order(
        make_order(market_id="house-race-b", quantity=400, price=0.5, idempotency_key="o2")
    )

    fits = make_order(market_id="governor-race-c", quantity=200, price=0.5, idempotency_key="o3")  # +100 -> 500
    assert manager.check(fits, fair_value=0.5, outside_data_age_seconds=0).approved

    manager.record_order(fits)
    too_much = make_order(market_id="senate-race-d", quantity=20, price=0.5, idempotency_key="o4")  # +10 -> 510
    decision = manager.check(too_much, fair_value=0.5, outside_data_age_seconds=0)
    assert not decision.approved
    assert "party" in decision.reason.lower()


def test_different_parties_do_not_share_the_cap(manager):
    manager.record_order(
        make_order(party_id="GOP", quantity=800, price=0.5, idempotency_key="o1")  # 400 notional
    )
    # A different party, same bankroll, same cap fraction — independent budget.
    other_party = make_order(party_id="DEM", market_id="senate-race-e", quantity=800, price=0.5, idempotency_key="o2")
    assert manager.check(other_party, fair_value=0.5, outside_data_age_seconds=0).approved


def test_order_without_party_id_is_not_subject_to_party_cap(manager):
    # An economic-indicator market with no party mapping shouldn't be
    # blocked by a limit that doesn't apply to it.
    manager.record_order(make_order(party_id="GOP", quantity=1000, price=0.5, idempotency_key="o1"))  # 500, at cap
    no_party_order = make_order(party_id=None, market_id="unemployment-rate", quantity=1000, price=0.5, idempotency_key="o2")
    assert manager.check(no_party_order, fair_value=0.5, outside_data_age_seconds=0).approved


def test_party_exposure_rejection_is_logged(manager):
    manager.record_order(make_order(quantity=1000, price=0.5, idempotency_key="o1"))  # at cap: 500
    over = make_order(market_id="house-race-f", quantity=20, price=0.5, idempotency_key="o2")

    manager.check(over, fair_value=0.5, outside_data_age_seconds=0)

    events = manager._event_store.all_events(event_type="risk_rejection")
    assert len(events) == 1
    assert events[0]["payload"]["party_id"] == "GOP"
