"""Regression guard: exposure must count an order until the venue has
*confirmed* it cancelled or expired — never from a locally-computed guess
(an `open` flag we haven't re-checked, or a past `expiration_date`).

docs/platform/SUMMARY.md found that the platform's `open` flag can stay
true past an order's expirationDate, and that expiry emits no realtime
event. If a future change "optimizes" exposure tracking by trusting
`order.expiration_date < now()` instead of waiting for an explicit
reconciliation read, this file must start failing.
"""

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from predcup.models import Order, OrderStatus
from predcup.risk import RiskLimits, RiskManager, is_exposure_counted
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
        max_bankroll_fraction_per_market=0.5,  # bankroll 1000 -> cap 500
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
    )


@pytest.mark.parametrize(
    "status,expected_counted",
    [
        (OrderStatus.PENDING, True),
        (OrderStatus.OPEN, True),
        (OrderStatus.FILLED, True),
        (OrderStatus.CANCELLED, False),
        (OrderStatus.EXPIRED, False),
        (OrderStatus.REJECTED, False),  # never went live — no shares, no exposure
    ],
)
def test_is_exposure_counted_table(status, expected_counted):
    assert is_exposure_counted(status) is expected_counted


def test_order_past_local_expiration_still_counts_until_confirmed(manager):
    past_expiry = datetime.now(timezone.utc) - timedelta(hours=1)
    order = make_order(
        quantity=800, price=0.5, expiration_date=past_expiry, status=OrderStatus.OPEN
    )  # notional 400, still under the 500 cap on its own
    manager.record_order(order)

    # A second order that would only fit if the expired-looking one had
    # already stopped counting (400 + 200 = 600 > 500 cap).
    more = make_order(quantity=400, price=0.5, idempotency_key="order-2")
    decision = manager.check(more, fair_value=0.5, outside_data_age_seconds=0)

    assert not decision.approved
    assert "market" in decision.reason.lower()


def test_confirmed_cancelled_frees_exposure(manager):
    order = make_order(quantity=800, price=0.5, idempotency_key="order-1")
    placed = order.model_copy(update={"id": "v-1"})
    manager.record_order(placed)

    more = make_order(quantity=400, price=0.5, idempotency_key="order-2")
    assert not manager.check(more, fair_value=0.5, outside_data_age_seconds=0).approved

    # Only an explicit, venue-confirmed state transition frees the exposure.
    manager.confirm_order_state("v-1", OrderStatus.CANCELLED)

    assert manager.check(more, fair_value=0.5, outside_data_age_seconds=0).approved


def test_confirmed_expired_frees_exposure(manager):
    order = make_order(quantity=800, price=0.5, idempotency_key="order-1")
    placed = order.model_copy(update={"id": "v-1"})
    manager.record_order(placed)

    manager.confirm_order_state("v-1", OrderStatus.EXPIRED)

    more = make_order(quantity=400, price=0.5, idempotency_key="order-2")
    assert manager.check(more, fair_value=0.5, outside_data_age_seconds=0).approved


def test_todo_api_marker_present_near_exposure_semantics():
    import predcup.risk as risk_module

    source = inspect.getsource(risk_module)
    assert "# TODO(api): verify live" in source
