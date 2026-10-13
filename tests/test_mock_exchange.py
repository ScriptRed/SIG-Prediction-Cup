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


# --- fills, for reconciliation and the multi-hour mock run ---------------------


def test_simulate_fill_updates_positions_fills_and_order():
    exchange = MockExchange()
    placed = run(exchange.place_order(make_order(side="yes", action="buy", quantity=10)))
    run(exchange.simulate_fill(placed.id, 4))
    [pos] = run(exchange.get_positions(TOURNAMENT_A))
    assert pos.quantity == 4
    fills = run(exchange.get_new_fills(TOURNAMENT_A, known_ids=set()))
    assert [(f.side, f.action, f.quantity) for f in fills] == [("yes", "buy", 4)]
    assert run(exchange.get_new_fills(TOURNAMENT_A, known_ids={fills[0].id})) == []
    [still_open] = run(exchange.get_open_orders(TOURNAMENT_A))
    assert still_open.quantity == 6


def test_sell_yes_fill_reports_as_no_side_and_nets():
    exchange = MockExchange()
    b = run(exchange.place_order(make_order(side="yes", action="buy", quantity=10, idempotency_key="b")))
    s = run(exchange.place_order(make_order(side="yes", action="sell", quantity=4, idempotency_key="s")))
    run(exchange.simulate_fill(b.id, 10))
    run(exchange.simulate_fill(s.id, 4))
    [pos] = run(exchange.get_positions(TOURNAMENT_A))
    assert pos.quantity == 6
    fills = run(exchange.get_new_fills(TOURNAMENT_A, known_ids=set()))
    assert [(f.side, f.quantity) for f in fills] == [("yes", 10), ("no", 4)]


def test_full_fill_removes_order():
    exchange = MockExchange()
    placed = run(exchange.place_order(make_order(quantity=5)))
    run(exchange.simulate_fill(placed.id, 5))
    assert run(exchange.get_open_orders(TOURNAMENT_A)) == []


def test_top_of_books_from_configured_books():
    exchange = MockExchange()
    exchange.set_top_of_book("36", 0.4, 0.45)
    assert run(exchange.get_top_of_books(["36", "37"], TOURNAMENT_A)) == {"36": (0.4, 0.45), "37": (None, None)}


def test_portfolio_reads_can_be_slowed_without_slowing_the_others():
    import time

    exchange = MockExchange(portfolio_delay=lambda: 0.15)
    start = time.monotonic()
    run(exchange.get_positions(TOURNAMENT_A))
    run(exchange.get_new_fills(TOURNAMENT_A, known_ids=set()))
    run(exchange.get_pnl(TOURNAMENT_A, period="day"))
    assert time.monotonic() - start >= 0.45
    start = time.monotonic()
    run(exchange.get_top_of_books(["36"], TOURNAMENT_A))
    run(exchange.get_open_orders(TOURNAMENT_A))
    assert time.monotonic() - start < 0.05


# --- 2026-10-13 soak mode: the mock enforces what the live platform did ----------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from predcup.venues.sig import SigOrderRejected, SigRateLimited  # noqa: E402


class _Clock:
    def __init__(self):
        self.mono = 0.0
        self.wall = datetime(2026, 10, 13, 16, 7, tzinfo=timezone.utc)

    def advance(self, s):
        self.mono += s
        self.wall += timedelta(seconds=s)


def _strict(clock, limit=(3, 60.0), **kw):
    return MockExchange(cancel_rate_limit=limit, retry_after_seconds=60.0, strict_orders=True,
                        mono=lambda: clock.mono, now=lambda: clock.wall, **kw)  # fmt: skip


def _o(clock, key="k", expires_in=120.0, **kw):
    fields = dict(exchange_id="e1", market_id="m1", tournament_id="T", side="yes", action="buy", quantity=5,
                  price=0.5, idempotency_key=key, expiration_date=clock.wall + timedelta(seconds=expires_in))  # fmt: skip
    fields.update(kw)
    return Order(**fields)


def test_strict_mock_rate_limits_cancel_all_and_counts_early_retries():
    clock = _Clock()
    ex = _strict(clock)
    for _ in range(3):
        asyncio.run(ex.cancel_all("T", exchange_id="e1"))
    with pytest.raises(SigRateLimited) as e:
        asyncio.run(ex.cancel_all("T", exchange_id="e1"))
    assert e.value.retry_after == 60.0 and ex.stats["cancel_429"] == 1
    clock.advance(30)
    with pytest.raises(SigRateLimited):
        asyncio.run(ex.cancel_all("T", exchange_id="e2"))
    assert ex.stats["retry_after_violations"] == 1  # called again before Retry-After passed
    clock.advance(30)
    asyncio.run(ex.cancel_all("T", exchange_id="e1"))
    assert ex.stats["retry_after_violations"] == 1


def test_strict_mock_tournament_wide_cancel_is_counted_but_never_refused():
    clock = _Clock()
    ex = _strict(clock, limit=(1, 60.0))
    asyncio.run(ex.cancel_all("T", exchange_id="e1"))
    assert asyncio.run(ex.cancel_all("T")).remaining == 0  # the kill switch (adapter waits Retry-After out)
    assert ex.stats["cancel_all_calls"] == 2


def test_strict_mock_rejects_the_whole_batch_if_any_order_is_invalid():
    clock = _Clock()
    ex = _strict(clock)
    good, expired = _o(clock, "a"), _o(clock, "b", expires_in=-31)
    with pytest.raises(SigOrderRejected) as e:
        asyncio.run(ex.place_batch([good, expired], "batch"))
    assert e.value.status == 400 and e.value.code == "VALIDATION_ERROR"
    assert e.value.details["orders"][0]["index"] == 1
    assert asyncio.run(ex.get_open_orders("T")) == [] and ex.stats["invalid_batches"] == 1
    for bad in (_o(clock).model_copy(update={"quantity": 0}), _o(clock).model_copy(update={"price": 0.4925})):
        with pytest.raises(SigOrderRejected):
            asyncio.run(ex.place_batch([bad], "b2"))


def test_strict_mock_orders_expire():
    clock = _Clock()
    ex = _strict(clock)
    asyncio.run(ex.place_batch([_o(clock, expires_in=20)], "b"))
    assert len(asyncio.run(ex.get_open_orders("T"))) == 1
    clock.advance(20)
    assert asyncio.run(ex.get_open_orders("T")) == []
    assert ex.stats["expired"] == 1


def test_strict_mock_concurrent_cancels_sent_before_a_429_are_not_violations():
    clock = _Clock()
    hits = iter([True, False, False])
    ex = _strict(clock, limit=(50, 60.0), inject_cancel_429=lambda: next(hits))

    async def burst():
        return await asyncio.gather(*(ex.cancel_all("T", exchange_id=e) for e in ("e1", "e2", "e3")),
                                    return_exceptions=True)  # fmt: skip

    results = asyncio.run(burst())
    assert isinstance(results[0], SigRateLimited) and not isinstance(results[1], Exception)
    assert ex.stats["retry_after_violations"] == 0
    with pytest.raises(SigRateLimited):  # a call after the 429 arrived is early
        asyncio.run(ex.cancel_all("T", exchange_id="e4"))
    assert ex.stats["retry_after_violations"] == 1
