from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from predcup.models import (
    Fill,
    Market,
    Order,
    OrderBook,
    OrderBookLevel,
    OrderStatus,
    Position,
    is_on_tick,
)

TOURNAMENT_ID = "550e8400-e29b-41d4-a716-446655440000"


def make_order(**overrides):
    fields = dict(
        exchange_id="36",
        tournament_id=TOURNAMENT_ID,
        side="yes",
        action="buy",
        quantity=100,
        price=0.42,
        idempotency_key="order-2026-01-01-001",
    )
    fields.update(overrides)
    return Order(**fields)


def test_order_requires_tournament_id():
    fields = dict(
        exchange_id="36",
        side="yes",
        action="buy",
        quantity=100,
        price=0.42,
        idempotency_key="order-2026-01-01-001",
    )
    with pytest.raises(ValidationError):
        Order(**fields)


def test_order_tournament_id_cannot_be_blank():
    with pytest.raises(ValidationError):
        make_order(tournament_id="")


def test_order_accepts_on_tick_price():
    order = make_order(price=0.420)
    assert order.price == 0.420


@pytest.mark.parametrize("price", [0.4231, 0.001, 1.0001, -0.01])
def test_order_rejects_off_tick_price(price):
    with pytest.raises(ValidationError):
        make_order(price=price)


def test_order_rejects_price_below_min_tick():
    with pytest.raises(ValidationError):
        make_order(price=0.0025)


def test_order_market_buy_marker_price_one_accepted():
    order = make_order(action="buy", price=1.0)
    assert order.price == 1.0


def test_order_market_sell_marker_price_zero_accepted():
    order = make_order(action="sell", price=0.0)
    assert order.price == 0.0


def test_order_buy_price_zero_is_off_tick_not_a_market_marker():
    # price: 0 only means "market order" for a sell, per spec. For a buy it's
    # off-tick (0 is below the 0.005 minimum) and must be rejected.
    with pytest.raises(ValidationError):
        make_order(action="buy", price=0.0)


def test_order_sell_price_one_is_off_tick_not_a_market_marker():
    with pytest.raises(ValidationError):
        make_order(action="sell", price=1.0)


def test_order_omitted_price_is_market_order():
    order = make_order(price=None)
    assert order.price is None


def test_order_quantity_must_be_positive():
    with pytest.raises(ValidationError):
        make_order(quantity=0)
    with pytest.raises(ValidationError):
        make_order(quantity=-5)


def test_order_quantity_exceeds_int32_rejected():
    with pytest.raises(ValidationError):
        make_order(quantity=2_147_483_648)


def test_order_quantity_at_int32_max_accepted():
    order = make_order(quantity=2_147_483_647)
    assert order.quantity == 2_147_483_647


def test_order_expiration_date_rejected_on_market_order():
    with pytest.raises(ValidationError):
        make_order(price=None, expiration_date=datetime(2026, 7, 13, tzinfo=timezone.utc))


def test_order_default_status_is_pending():
    order = make_order()
    assert order.status == OrderStatus.PENDING


def test_order_party_id_and_race_key_default_to_none():
    order = make_order()
    assert order.party_id is None
    assert order.race_key is None


def test_order_party_id_and_race_key_accepted():
    order = make_order(party_id="R", race_key="MI-Senate")
    assert order.party_id == "R"
    assert order.race_key == "MI-Senate"


def test_is_on_tick_helper():
    assert is_on_tick(0.005)
    assert is_on_tick(0.995)
    assert is_on_tick(0.5)
    assert not is_on_tick(0.5031)
    assert not is_on_tick(0.004)
    assert not is_on_tick(0.996)


def test_fill_requires_tournament_id():
    with pytest.raises(ValidationError):
        Fill(
            id="f1",
            order_id="o1",
            exchange_id="36",
            side="yes",
            action="buy",
            quantity=100,
            price=0.42,
            filled_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        )


def test_fill_valid():
    fill = Fill(
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
    assert fill.tournament_id == TOURNAMENT_ID


def test_position_requires_tournament_id():
    with pytest.raises(ValidationError):
        Position(
            exchange_id="36",
            market_id="26",
            quantity=200,
            avg_cost=0.42,
        )


def test_position_negative_quantity_means_no_shares():
    position = Position(
        exchange_id="36",
        market_id="26",
        tournament_id=TOURNAMENT_ID,
        quantity=-50,
        avg_cost=0.3,
    )
    assert position.quantity == -50


def test_market_status_enum_rejects_unknown_value():
    with pytest.raises(ValidationError):
        Market(id="26", title="Test market", status="paused")


def test_market_valid():
    market = Market(id="26", title="Test market", status="open")
    assert market.status == "open"


def test_order_book_level_and_book():
    book = OrderBook(
        exchange_id="36",
        tournament_id=TOURNAMENT_ID,
        bids=[OrderBookLevel(price=0.42, quantity=100)],
        asks=[OrderBookLevel(price=0.44, quantity=50)],
    )
    assert book.bids[0].price == 0.42
    assert book.asks[0].quantity == 50
