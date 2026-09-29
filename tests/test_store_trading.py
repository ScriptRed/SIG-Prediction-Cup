"""orders / fills / positions tables: the local side of reconciliation.

Local positions are derived only from fills, as signed YES-share
quantities (+ = YES, − = NO, same sign convention as the platform's
Position.quantity). Netting falls out of the sign: holding YES and buying
NO reduces the position share-for-share, which is what the engine does
(CLAUDE.md "Netting").
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from predcup.models import Fill, Order, OrderStatus
from predcup.store import EventStore, signed_fill_quantity

TOURNAMENT_ID = "550e8400-e29b-41d4-a716-446655440000"
OTHER_TOURNAMENT = "11111111-2222-3333-4444-555555555555"
T0 = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)


def make_order(**overrides):
    fields = dict(
        exchange_id="36",
        market_id="market-a",
        tournament_id=TOURNAMENT_ID,
        side="yes",
        action="buy",
        quantity=100,
        price=0.5,
        idempotency_key="key-1",
    )
    fields.update(overrides)
    return Order(**fields)


_fill_ids = iter(range(1, 10_000))


def make_fill(**overrides):
    fields = dict(
        id=f"fill-{next(_fill_ids)}",
        order_id="ord-1",
        exchange_id="36",
        tournament_id=TOURNAMENT_ID,
        side="yes",
        action="buy",
        quantity=10,
        price=0.5,
        filled_at=T0,
    )
    fields.update(overrides)
    return Fill(**fields)


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "predcup.db")
    yield s
    s.close()


def test_trading_tables_created(store, tmp_path):
    conn = sqlite3.connect(tmp_path / "predcup.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"orders", "fills", "positions"} <= tables


# --- signed quantities / netting --------------------------------------------


@pytest.mark.parametrize(
    "side, action, expected",
    [("yes", "buy", 10), ("yes", "sell", -10), ("no", "buy", -10), ("no", "sell", 10)],
)
def test_signed_fill_quantity(side, action, expected):
    assert signed_fill_quantity(make_fill(side=side, action=action, quantity=10)) == expected


def test_buying_no_while_holding_yes_nets_down(store):
    store.record_fill(make_fill(side="yes", action="buy", quantity=100))
    store.record_fill(make_fill(side="no", action="buy", quantity=30))
    assert store.local_positions(TOURNAMENT_ID) == {"36": 70}


def test_netting_through_zero_flips_to_no(store):
    store.record_fill(make_fill(side="yes", action="buy", quantity=20))
    store.record_fill(make_fill(side="no", action="buy", quantity=50))
    assert store.local_positions(TOURNAMENT_ID) == {"36": -30}


def test_flat_position_is_reported_as_zero_not_dropped(store):
    store.record_fill(make_fill(side="yes", action="buy", quantity=20))
    store.record_fill(make_fill(side="yes", action="sell", quantity=20))
    assert store.local_positions(TOURNAMENT_ID) == {"36": 0}


# --- fills ------------------------------------------------------------------


def test_duplicate_fill_is_ignored(store):
    # Same fill can arrive from the websocket and from a REST resync.
    fill = make_fill(quantity=10)
    assert store.record_fill(fill) is True
    assert store.record_fill(fill) is False
    assert store.local_positions(TOURNAMENT_ID) == {"36": 10}
    assert len(store.fills(TOURNAMENT_ID)) == 1


def test_fills_round_trip(store):
    fill = make_fill(side="no", action="sell", quantity=7, price=0.335)
    store.record_fill(fill)
    [back] = store.fills(TOURNAMENT_ID)
    assert back == fill


def test_positions_are_isolated_per_tournament(store):
    store.record_fill(make_fill(quantity=10))
    store.record_fill(make_fill(quantity=99, tournament_id=OTHER_TOURNAMENT))
    assert store.local_positions(TOURNAMENT_ID) == {"36": 10}
    assert store.local_positions(OTHER_TOURNAMENT) == {"36": 99}


def test_positions_survive_reconnect(tmp_path):
    db = tmp_path / "predcup.db"
    s1 = EventStore(db)
    s1.record_fill(make_fill(quantity=10, exchange_id="36"))
    s1.record_fill(make_fill(quantity=5, exchange_id="37", side="no"))
    s1.close()
    s2 = EventStore(db)
    assert s2.local_positions(TOURNAMENT_ID) == {"36": 10, "37": -5}
    s2.close()


def test_positions_table_matches_replayed_fills(store):
    # The positions table is a cache; it must always equal a replay of fills.
    for side, action, q in [("yes", "buy", 40), ("no", "buy", 15), ("yes", "sell", 5), ("no", "sell", 3)]:
        store.record_fill(make_fill(side=side, action=action, quantity=q))
    assert store.local_positions(TOURNAMENT_ID) == store.replay_positions_from_fills(TOURNAMENT_ID)
    assert store.local_positions(TOURNAMENT_ID) == {"36": 40 - 15 - 5 + 3}


# --- orders -----------------------------------------------------------------


def test_order_upsert_and_lookup_by_idempotency_key(store):
    order = make_order()
    store.upsert_order(order)
    assert store.get_order("key-1") == order


def test_order_status_update_after_venue_ack(store):
    store.upsert_order(make_order())
    acked = make_order(id="ord-77", status=OrderStatus.OPEN, created_at=T0)
    store.upsert_order(acked)
    back = store.get_order("key-1")
    assert back.id == "ord-77"
    assert back.status == OrderStatus.OPEN
    assert store.get_order_by_venue_id("ord-77") == back


def test_order_round_trip_keeps_all_fields(store):
    order = make_order(
        id="ord-1",
        party_id="R",
        race_key="PA-SEN",
        side="no",
        action="sell",
        quantity=12,
        price=0.625,
        expiration_date=T0 + timedelta(seconds=30),
        status=OrderStatus.CANCELLED,
        created_at=T0,
        terminal_reason_code="SelfTradePrevented",
    )
    store.upsert_order(order)
    assert store.get_order("key-1") == order


def test_market_order_price_none_round_trips(store):
    store.upsert_order(make_order(price=None))
    assert store.get_order("key-1").price is None


def test_orders_with_status_filters(store):
    store.upsert_order(make_order(idempotency_key="a", status=OrderStatus.OPEN))
    store.upsert_order(make_order(idempotency_key="b", status=OrderStatus.PENDING))
    store.upsert_order(make_order(idempotency_key="c", status=OrderStatus.CANCELLED))
    store.upsert_order(make_order(idempotency_key="d", status=OrderStatus.OPEN, tournament_id=OTHER_TOURNAMENT))
    keys = {
        o.idempotency_key
        for o in store.orders_with_status(TOURNAMENT_ID, [OrderStatus.OPEN, OrderStatus.PENDING])
    }
    assert keys == {"a", "b"}


def test_missing_order_returns_none(store):
    assert store.get_order("nope") is None
    assert store.get_order_by_venue_id("nope") is None
