"""Kill switch: scoped cancel-all, then confirm via get_open_orders (our
stand-in for GET /orders?status=open scoped to the Cup). If anything
remains, retry and alert — don't trust the cancel-all response alone.
"""

import asyncio

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


def run(coro):
    return asyncio.run(coro)


def make_manager(venue, alerter=None, sleep=None):
    limits = RiskLimits(
        max_bankroll_fraction_per_market=1.0,
        max_total_exposure_fraction=1.0,
        max_party_exposure_fraction=1.0,
        max_order_size_susqies=1_000_000,
        max_price_deviation_from_fair_value=1.0,
        daily_loss_stop_fraction=1.0,
        stale_data_stop_seconds=60,
    )
    kwargs = dict(
        limits=limits,
        bankroll=1000.0,
        event_store=EventStore(":memory:"),
        venue=venue,
        tournament_id=TOURNAMENT_ID,
        alerter=alerter or FakeAlerter(),
        size_ramp=full_size_ramp(),
    )
    if sleep is not None:
        kwargs["sleep"] = sleep
    return RiskManager(**kwargs)


async def no_sleep(_seconds: float) -> None:
    return None


def test_kill_switch_clean_cancel_sends_no_alert():
    venue = MockExchange()
    run(venue.place_order(make_order(idempotency_key="o1")))
    run(venue.place_order(make_order(idempotency_key="o2")))
    alerter = FakeAlerter()
    manager = make_manager(venue, alerter=alerter, sleep=no_sleep)

    result = run(manager.kill_switch())

    assert result.success
    assert result.attempts == 1
    assert result.remaining_order_ids == []
    assert alerter.messages == []
    assert run(venue.get_open_orders(TOURNAMENT_ID)) == []


def test_kill_switch_detects_silent_miss_and_alerts_immediately():
    venue = MockExchange()
    placed = run(venue.place_order(make_order()))
    # Miss forever: simulates a persistent venue-side bug, not a transient
    # blip, so we can assert the escalation path deterministically.
    venue.configure_cancel_all_to_silently_miss({placed.id})
    alerter = FakeAlerter()
    manager = make_manager(venue, alerter=alerter, sleep=no_sleep)

    result = run(manager.kill_switch(max_attempts=2))

    assert not result.success
    assert result.remaining_order_ids == [placed.id]
    # Alerted at least once about the discrepancy, not just silently retried.
    assert len(alerter.messages) >= 1
    assert any("open" in m.lower() for m in alerter.messages)


def test_kill_switch_recovers_after_a_transient_miss():
    venue = MockExchange()
    placed = run(venue.place_order(make_order()))
    # Miss exactly once: the first cancel-all silently fails, the retry
    # succeeds — the kill switch must keep trying, not give up after one
    # bad confirmation.
    venue.configure_cancel_all_to_silently_miss({placed.id}, times=1)
    alerter = FakeAlerter()
    manager = make_manager(venue, alerter=alerter, sleep=no_sleep)

    result = run(manager.kill_switch(max_attempts=3))

    assert result.success
    assert result.attempts == 2
    assert run(venue.get_open_orders(TOURNAMENT_ID)) == []
    # Still alerted about the first attempt's discrepancy even though it
    # self-healed on retry — a human should know it happened at all.
    assert len(alerter.messages) >= 1


def test_kill_switch_scopes_to_exchange_id():
    venue = MockExchange()
    run(venue.place_order(make_order(idempotency_key="o1", exchange_id="36")))
    run(venue.place_order(make_order(idempotency_key="o2", exchange_id="37")))
    manager = make_manager(venue, sleep=no_sleep)

    result = run(manager.kill_switch(exchange_id="36"))

    assert result.success
    remaining = run(venue.get_open_orders(TOURNAMENT_ID))
    assert len(remaining) == 1
    assert remaining[0].exchange_id == "37"


def test_kill_switch_events_are_logged():
    venue = MockExchange()
    run(venue.place_order(make_order()))
    manager = make_manager(venue, sleep=no_sleep)

    run(manager.kill_switch())

    events = manager._event_store.all_events(event_type="kill_switch")
    assert len(events) >= 1
    assert events[-1]["payload"]["result"] == "clean"
