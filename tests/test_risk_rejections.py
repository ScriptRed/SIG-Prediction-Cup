"""Any unexpected 4xx on an order (possible position limit) stops quoting
in that market and alerts — never retried. CLAUDE.md's retry rules already
say 4xx besides 429/409 REQUEST_IN_FLIGHT must never be auto-retried, so by
the time something reaches record_order_rejection it's terminal — this is
purely a halt-and-alert bookkeeping step, not a retry path.
"""

import inspect

import pytest

from _helpers import full_size_ramp
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
def manager(tmp_path):
    limits = RiskLimits(
        max_bankroll_fraction_per_market=1.0,
        max_total_exposure_fraction=1.0,
        max_party_exposure_fraction=1.0,
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
        size_ramp=full_size_ramp(),
    )


def test_record_order_rejection_is_not_a_coroutine():
    # Structural: it has no way to await a venue call, so it cannot retry
    # anything by construction.
    assert not inspect.iscoroutinefunction(RiskManager.record_order_rejection)


def test_unexpected_4xx_halts_the_market(manager):
    assert not manager.is_market_halted("market-a")

    manager.record_order_rejection(
        market_id="market-a",
        status_code=422,
        error_code="UNKNOWN_LIMIT",
        message="Order rejected for an undocumented reason",
    )

    assert manager.is_market_halted("market-a")


def test_unexpected_4xx_sends_an_alert(manager):
    manager.record_order_rejection(
        market_id="market-a", status_code=403, error_code="FORBIDDEN", message="nope"
    )

    assert len(manager._alerter.messages) == 1
    message = manager._alerter.messages[0]
    assert "market-a" in message
    assert "403" in message


def test_halted_market_rejects_subsequent_orders(manager):
    manager.record_order_rejection(market_id="market-a", status_code=422, error_code="X", message="m")

    order = make_order(market_id="market-a")
    decision = manager.check(order, fair_value=0.5, outside_data_age_seconds=0)

    assert not decision.approved
    assert "halt" in decision.reason.lower()


def test_other_markets_are_unaffected(manager):
    manager.record_order_rejection(market_id="market-a", status_code=422, error_code="X", message="m")

    order = make_order(market_id="market-b")
    decision = manager.check(order, fair_value=0.5, outside_data_age_seconds=0)

    assert decision.approved
    assert not manager.is_market_halted("market-b")


def test_rejection_is_logged_with_status_and_message(manager):
    manager.record_order_rejection(
        market_id="market-a", status_code=422, error_code="UNKNOWN_LIMIT", message="Order rejected"
    )

    events = manager._event_store.all_events(event_type="market_halted")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["market_id"] == "market-a"
    assert payload["status_code"] == 422
    assert payload["error_code"] == "UNKNOWN_LIMIT"
    assert payload["message"] == "Order rejected"
