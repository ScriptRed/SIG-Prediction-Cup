import asyncio
import inspect

import pytest

from predcup.models import Order
from sim.mock_exchange import MockExchange

TOURNAMENT_A = "550e8400-e29b-41d4-a716-446655440000"
TOURNAMENT_B = "660e8400-e29b-41d4-a716-446655440000"


def make_order(**overrides):
    fields = dict(
        exchange_id="36",
        market_id="26",
        tournament_id=TOURNAMENT_A,
        side="yes",
        action="buy",
        quantity=100,
        price=0.42,
        idempotency_key="order-2026-01-01-001",
    )
    fields.update(overrides)
    return Order(**fields)


def run(coro):
    return asyncio.run(coro)


def test_get_positions_without_tournament_id_raises_type_error():
    exchange = MockExchange()
    with pytest.raises(TypeError):
        run(exchange.get_positions())


def test_get_open_orders_without_tournament_id_raises_type_error():
    exchange = MockExchange()
    with pytest.raises(TypeError):
        run(exchange.get_open_orders())


def test_cancel_all_without_tournament_id_raises_type_error():
    exchange = MockExchange()
    with pytest.raises(TypeError):
        run(exchange.cancel_all())


def test_place_order_then_appears_in_open_orders():
    exchange = MockExchange()
    order = make_order()

    placed = run(exchange.place_order(order))

    assert placed.id is not None
    assert placed.status == "open"
    open_orders = run(exchange.get_open_orders(TOURNAMENT_A))
    assert len(open_orders) == 1
    assert open_orders[0].id == placed.id


def test_orders_are_isolated_per_tournament():
    exchange = MockExchange()
    run(exchange.place_order(make_order(tournament_id=TOURNAMENT_A)))
    run(exchange.place_order(make_order(tournament_id=TOURNAMENT_B, idempotency_key="order-2")))

    assert len(run(exchange.get_open_orders(TOURNAMENT_A))) == 1
    assert len(run(exchange.get_open_orders(TOURNAMENT_B))) == 1


def test_cancel_single_order():
    exchange = MockExchange()
    placed = run(exchange.place_order(make_order()))

    run(exchange.cancel(placed.id, TOURNAMENT_A))

    assert run(exchange.get_open_orders(TOURNAMENT_A)) == []


def test_cancel_all_cancels_every_open_order_in_scope():
    exchange = MockExchange()
    run(exchange.place_order(make_order(idempotency_key="o1")))
    run(exchange.place_order(make_order(idempotency_key="o2", exchange_id="37")))

    result = run(exchange.cancel_all(TOURNAMENT_A))

    assert result.cancelled == 2
    assert result.remaining == 0
    assert result.all_cancelled
    assert run(exchange.get_open_orders(TOURNAMENT_A)) == []


def test_cancel_all_scoped_by_exchange_id_leaves_others_open():
    exchange = MockExchange()
    run(exchange.place_order(make_order(idempotency_key="o1", exchange_id="36")))
    run(exchange.place_order(make_order(idempotency_key="o2", exchange_id="37")))

    result = run(exchange.cancel_all(TOURNAMENT_A, exchange_id="36"))

    assert result.cancelled == 1
    remaining = run(exchange.get_open_orders(TOURNAMENT_A))
    assert len(remaining) == 1
    assert remaining[0].exchange_id == "37"


def test_cancel_all_does_not_touch_other_tournaments():
    exchange = MockExchange()
    run(exchange.place_order(make_order(tournament_id=TOURNAMENT_A)))
    run(exchange.place_order(make_order(tournament_id=TOURNAMENT_B, idempotency_key="o2")))

    run(exchange.cancel_all(TOURNAMENT_A))

    assert run(exchange.get_open_orders(TOURNAMENT_A)) == []
    assert len(run(exchange.get_open_orders(TOURNAMENT_B))) == 1


def test_cancel_all_can_be_configured_to_silently_miss_an_order():
    """Simulates the documented CancelAllPausedError-adjacent failure mode:
    the venue reports success but an order is still resting. Callers (the
    kill switch) must not trust the response alone — they must confirm via
    get_open_orders.
    """
    exchange = MockExchange()
    placed = run(exchange.place_order(make_order()))
    exchange.configure_cancel_all_to_silently_miss({placed.id})

    result = run(exchange.cancel_all(TOURNAMENT_A))

    # The mock claims success just like a real silent-miss would...
    assert result.all_cancelled
    # ...but the order is still actually open underneath.
    still_open = run(exchange.get_open_orders(TOURNAMENT_A))
    assert len(still_open) == 1
    assert still_open[0].id == placed.id


def test_get_balance_and_get_markets_require_tournament_id():
    for name in ("get_balance", "get_markets"):
        sig = inspect.signature(getattr(MockExchange, name))
        assert sig.parameters["tournament_id"].default is inspect.Parameter.empty
