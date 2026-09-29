"""Markouts at 1, 5 and 30 minutes after every fill, logged to events_log —
the only way to answer "was this fill actually good?" after the fact
(docs/LAUNCH_CHECKLIST.md D: "Markouts at 1 / 5 / 30 min | From
`events_log` after first fills").
"""

import asyncio
from datetime import datetime, timezone

import pytest

from _helpers import full_size_ramp
from predcup.models import Fill
from predcup.risk import RiskLimits, RiskManager, compute_markout
from predcup.store import EventStore
from sim.mock_exchange import MockExchange

TOURNAMENT_ID = "550e8400-e29b-41d4-a716-446655440000"


class FakeAlerter:
    def send(self, message: str) -> None:
        pass


def run(coro):
    return asyncio.run(coro)


async def no_sleep(_seconds: float) -> None:
    return None


def make_manager(sleep=no_sleep):
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
        event_store=EventStore(":memory:"),
        venue=MockExchange(),
        tournament_id=TOURNAMENT_ID,
        alerter=FakeAlerter(),
        size_ramp=full_size_ramp(),
        sleep=sleep,
    )


def make_fill(**overrides):
    fields = dict(
        id="f1",
        order_id="o1",
        exchange_id="36",
        tournament_id=TOURNAMENT_ID,
        side="yes",
        action="buy",
        quantity=100,
        price=0.42,
        filled_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    fields.update(overrides)
    return Fill(**fields)


@pytest.mark.parametrize(
    "side,action,fill_price,later_price,expected_markout",
    [
        ("yes", "buy", 0.40, 0.45, 0.05),  # bought YES, price rose -> good
        ("yes", "buy", 0.40, 0.35, -0.05),  # bought YES, price fell -> bad
        ("yes", "sell", 0.60, 0.50, 0.10),  # sold YES, price fell -> good
        ("yes", "sell", 0.60, 0.70, -0.10),  # sold YES, price rose -> bad
        ("no", "buy", 0.40, 0.30, 0.10),  # bought NO, YES price fell -> good
        ("no", "sell", 0.40, 0.50, 0.10),  # sold NO, YES price rose -> good
    ],
)
def test_compute_markout_direction(side, action, fill_price, later_price, expected_markout):
    fill = make_fill(side=side, action=action, price=fill_price)
    assert compute_markout(fill, later_price) == pytest.approx(expected_markout)


def test_schedule_markouts_logs_all_three_horizons():
    manager = make_manager()
    fill = make_fill(price=0.40)

    async def price_lookup(_minutes: int) -> float:
        return 0.45

    async def scenario():
        tasks = manager.schedule_markouts(fill, price_lookup)
        await asyncio.gather(*tasks)

    run(scenario())

    events = manager._event_store.all_events(event_type="markout")
    assert len(events) == 3
    assert {e["payload"]["minutes"] for e in events} == {1, 5, 30}
    for e in events:
        assert e["payload"]["fill_id"] == "f1"
        assert e["payload"]["fill_price"] == 0.40
        assert e["payload"]["later_price"] == 0.45
        assert e["payload"]["markout"] == pytest.approx(0.05)


def test_schedule_markouts_uses_price_lookup_per_horizon():
    # price_lookup is called with the horizon itself, so which price landed
    # in which event never depends on task-scheduling order.
    manager = make_manager()
    fill = make_fill(price=0.40)
    prices_by_minutes = {1: 0.41, 5: 0.44, 30: 0.50}

    async def price_lookup(minutes: int) -> float:
        return prices_by_minutes[minutes]

    async def scenario():
        tasks = manager.schedule_markouts(fill, price_lookup)
        await asyncio.gather(*tasks)

    run(scenario())

    events = manager._event_store.all_events(event_type="markout")
    by_minutes = {e["payload"]["minutes"]: e["payload"]["later_price"] for e in events}
    assert by_minutes == prices_by_minutes
