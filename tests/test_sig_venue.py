"""predcup/venues/sig.py against a mock transport. Shapes follow
docs/platform/openapi.json; the retry rules are CLAUDE.md's: retry only
429, 503 and 409 REQUEST_IN_FLIGHT (plus 502 ORDER_STATUS_UNKNOWN with the
same key), always reusing the same idempotency key and payload."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from predcup.models import Order, OrderStatus
from predcup.venues.sig import (
    OrderStatusUnknown,
    SigApiError,
    SigOrderRejected,
    SigVenue,
    format_price,
    new_idempotency_key,
)

BASE = "https://sig.test/api/v1"
SLUG = "midterm-elections"
TID = "550e8400-e29b-41d4-a716-446655440000"


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)  # (status, json, headers) or callables
        self.requests: list[httpx.Request] = []
        self.tournament_reads = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/tournaments/{SLUG}") and request.method == "GET":
            self.tournament_reads += 1
            return httpx.Response(200, json={"id": TID, "slug": SLUG, "myBalance": 98765.5})
        self.requests.append(request)  # only the calls under test
        status, body, headers = self.responses.pop(0)
        return httpx.Response(status, json=body, headers=headers or {})

    def body(self, i: int) -> dict:
        return json.loads(self.requests[i].content)

    def non_tournament_requests(self) -> list[httpx.Request]:
        return self.requests


def run(coro):
    return asyncio.run(coro)


def make_venue(recorder, rate_limited=None):
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    venue = SigVenue(
        client,
        base_url=BASE,
        api_key="k",
        tournament_slug=SLUG,
        on_rate_limited=(rate_limited if rate_limited is not None else (lambda endpoint, retry_after: None)),
        sleep=fake_sleep,
        max_retries=4,
    )
    return venue, sleeps


def order(**kw) -> Order:
    fields = dict(
        exchange_id="1077", market_id="388", tournament_id=TID, side="yes", action="buy",
        quantity=10, price=0.305, idempotency_key="key-1",
        expiration_date=datetime(2026, 10, 1, 17, 0, 30, tzinfo=timezone.utc),
    )  # fmt: skip
    fields.update(kw)
    return Order(**fields)


PLACED = {"orderId": 1001, "exchangeId": "1077", "open": True, "remainingQuantity": 10,
          "quantityTraded": 0, "totalCost": 0, "fillPrice": None, "all": None}  # fmt: skip


def err(code: str, message: str = "x", **details) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


# --- construction / helpers -------------------------------------------------


def test_requires_rate_limit_callback():
    with pytest.raises(ValueError, match="on_rate_limited"):
        SigVenue(httpx.AsyncClient(), base_url=BASE, api_key="k", tournament_slug=SLUG, on_rate_limited=None)


def test_idempotency_keys_are_fresh():
    assert len({new_idempotency_key() for _ in range(1000)}) == 1000


@pytest.mark.parametrize("p,expected", [(0.305, 0.305), (0.1 + 0.2, 0.3), (0.3049999, 0.305), (0.005, 0.005), (0.995, 0.995)])
def test_format_price_rounds_to_tick(p, expected):
    assert format_price(p) == expected


def test_format_price_rejects_out_of_range():
    with pytest.raises(ValueError):
        format_price(1.2)


# --- place_order ------------------------------------------------------------


def test_place_order_body_and_result():
    rec = Recorder([(200, PLACED, None)])
    venue, _ = make_venue(rec)
    placed = run(venue.place_order(order()))
    body = rec.body(0)
    assert body == {
        "exchangeId": "1077", "side": "yes", "action": "buy", "quantity": 10, "price": 0.305,
        "tournamentId": TID, "idempotencyKey": "key-1", "expirationDate": "2026-10-01T17:00:30Z",
    }  # fmt: skip
    assert rec.requests[0].headers["Authorization"] == "Bearer k"
    assert placed.id == "1001" and placed.status == OrderStatus.OPEN


def test_fully_filled_order_is_filled():
    rec = Recorder([(200, {**PLACED, "open": False, "remainingQuantity": 0, "quantityTraded": 10}, None)])
    venue, _ = make_venue(rec)
    assert run(venue.place_order(order())).status == OrderStatus.FILLED


def test_429_retried_with_same_key_and_payload_and_reported():
    seen = []
    rec = Recorder([(429, err("RATE_LIMITED"), {"Retry-After": "2"}), (200, PLACED, None)])
    venue, sleeps = make_venue(rec, rate_limited=lambda endpoint, ra: seen.append((endpoint, ra)))
    run(venue.place_order(order()))
    assert rec.requests[0].content == rec.requests[1].content
    assert sleeps == [2.0]
    assert seen == [("POST /orders", 2.0)]


def test_503_retried_with_backoff():
    rec = Recorder([(503, err("SERVICE_UNAVAILABLE"), None), (200, PLACED, None)])
    venue, sleeps = make_venue(rec)
    run(venue.place_order(order()))
    assert len(rec.requests) == 2 and len(sleeps) == 1 and sleeps[0] > 0


def test_409_request_in_flight_retried_same_key():
    rec = Recorder([(409, err("REQUEST_IN_FLIGHT"), None), (200, PLACED, None)])
    venue, _ = make_venue(rec)
    run(venue.place_order(order()))
    assert rec.body(0) == rec.body(1)


def test_409_conflict_is_not_retried():
    rec = Recorder([(409, err("CONFLICT", "key reused"), None)])
    venue, _ = make_venue(rec)
    with pytest.raises(SigOrderRejected) as e:
        run(venue.place_order(order()))
    assert e.value.status == 409 and e.value.code == "CONFLICT"
    assert len(rec.requests) == 1


@pytest.mark.parametrize("status,code", [(400, "VALIDATION_ERROR"), (400, "INSUFFICIENT_BALANCE"), (403, "TERMS_NOT_ACKNOWLEDGED"), (404, "NOT_FOUND")])
def test_other_4xx_never_retried(status, code):
    rec = Recorder([(status, err(code), None)])
    venue, _ = make_venue(rec)
    with pytest.raises(SigOrderRejected) as e:
        run(venue.place_order(order()))
    assert (e.value.status, e.value.code) == (status, code)
    assert len(rec.requests) == 1


def test_502_status_unknown_retried_with_same_key_then_succeeds():
    rec = Recorder([(502, err("ORDER_STATUS_UNKNOWN"), None), (200, PLACED, None)])
    venue, _ = make_venue(rec)
    assert run(venue.place_order(order())).id == "1001"
    assert rec.body(0)["idempotencyKey"] == rec.body(1)["idempotencyKey"] == "key-1"


def test_502_persisting_raises_status_unknown_with_key():
    rec = Recorder([(502, err("ORDER_STATUS_UNKNOWN"), None)] * 5)
    venue, _ = make_venue(rec)
    with pytest.raises(OrderStatusUnknown) as e:
        run(venue.place_order(order()))
    assert e.value.idempotency_key == "key-1"
    assert {rec.body(i)["idempotencyKey"] for i in range(len(rec.requests))} == {"key-1"}


def test_429_exhausted_raises_api_error():
    rec = Recorder([(429, err("RATE_LIMITED"), None)] * 5)
    venue, _ = make_venue(rec)
    with pytest.raises(SigApiError) as e:
        run(venue.place_order(order()))
    assert e.value.status == 429


def test_order_for_another_tournament_is_refused():
    rec = Recorder([])
    venue, _ = make_venue(rec)
    with pytest.raises(ValueError, match="tournament"):
        run(venue.place_order(order(tournament_id="other-tournament")))
    assert rec.non_tournament_requests() == []


# --- batch ------------------------------------------------------------------


def _item(i, status=201, ok=True, **data):
    return {"index": i, "ok": ok, "status": status, "data": data}


def test_batch_body_and_results():
    rec = Recorder([(200, {"results": [_item(0, orderId=1, open=True), _item(1, orderId=2, open=False)]}, None)])
    venue, _ = make_venue(rec)
    res = run(venue.place_batch([order(idempotency_key="a"), order(idempotency_key="b", price=0.5)], batch_key="batch-1"))
    body = rec.body(0)
    assert body["idempotencyKey"] == "batch-1"
    assert [o["price"] for o in body["orders"]] == [0.305, 0.5]
    assert all(o["tournamentId"] == TID and "idempotencyKey" not in o for o in body["orders"])
    assert [(r.ok, r.order.id, r.order.status) for r in res] == [(True, "1", OrderStatus.OPEN), (True, "2", OrderStatus.FILLED)]


def test_batch_over_50_refused():
    venue, _ = make_venue(Recorder([]))
    with pytest.raises(ValueError, match="50"):
        run(venue.place_batch([order(idempotency_key=str(i)) for i in range(51)], batch_key="b"))


def test_batch_207_with_transient_item_resumes_with_same_key():
    partial = {"results": [_item(0, orderId=1, open=True), _item(1, status=429, ok=False, code="RATE_LIMITED")]}
    done = {"results": [_item(0, orderId=1, open=True), _item(1, orderId=2, open=True)]}
    rec = Recorder([(207, partial, {"Retry-After": "1"}), (200, done, None)])
    seen = []
    venue, _ = make_venue(rec, rate_limited=lambda e, ra: seen.append(e))
    res = run(venue.place_batch([order(idempotency_key="a"), order(idempotency_key="b")], batch_key="batch-2"))
    assert rec.requests[0].content == rec.requests[1].content
    assert all(r.ok for r in res)
    assert seen == ["POST /orders/batch"]


def test_batch_207_terminal_failure_is_returned_not_retried():
    res_body = {"results": [_item(0, orderId=1, open=True),
                            _item(1, status=400, ok=False, error={"code": "INSUFFICIENT_BALANCE", "message": "no"})]}  # fmt: skip
    rec = Recorder([(207, res_body, None)])
    venue, _ = make_venue(rec)
    res = run(venue.place_batch([order(idempotency_key="a"), order(idempotency_key="b")], batch_key="b3"))
    assert len(rec.requests) == 1
    assert [r.ok for r in res] == [True, False]
    assert res[1].status == 400 and res[1].code == "INSUFFICIENT_BALANCE"


def test_batch_all_unknown_502_retried_then_raises():
    body = {"results": [_item(0, status=502, ok=False, code="ORDER_STATUS_UNKNOWN")]}
    rec = Recorder([(502, body, None)] * 5)
    venue, _ = make_venue(rec)
    with pytest.raises(OrderStatusUnknown) as e:
        run(venue.place_batch([order()], batch_key="b4"))
    assert e.value.idempotency_key == "b4"


def test_batch_400_validation_not_retried():
    rec = Recorder([(400, err("VALIDATION_ERROR"), None)])
    venue, _ = make_venue(rec)
    with pytest.raises(SigOrderRejected):
        run(venue.place_batch([order()], batch_key="b5"))
    assert len(rec.requests) == 1


# --- cancel -----------------------------------------------------------------


def test_cancel_all_always_scoped_to_tournament():
    rec = Recorder([(200, {"cancelled": 3, "errors": []}, None), (200, {"cancelled": 1, "errors": []}, None)])
    venue, _ = make_venue(rec)
    r = run(venue.cancel_all(TID))
    assert rec.body(0) == {"tournamentId": TID}
    assert (r.cancelled, r.remaining, r.all_cancelled) == (3, 0, True)
    run(venue.cancel_all(TID, exchange_id="1077"))
    assert rec.body(1) == {"tournamentId": TID, "exchangeId": "1077"}


def test_cancel_all_exchange_and_market_are_exclusive():
    venue, _ = make_venue(Recorder([]))
    with pytest.raises(ValueError):
        run(venue.cancel_all(TID, exchange_id="1", market_id="2"))


def test_cancel_all_paused_503_retried_identically():
    paused = err("SERVICE_UNAVAILABLE", "paused", cancelled=1, errors=[], remaining=2)
    rec = Recorder([(503, paused, None), (200, {"cancelled": 2, "errors": []}, None)])
    venue, _ = make_venue(rec)
    r = run(venue.cancel_all(TID, market_id="388"))
    assert rec.requests[0].content == rec.requests[1].content
    assert r.all_cancelled


@pytest.mark.parametrize("status", [207, 422])
def test_cancel_all_partial_reports_remaining(status):
    rec = Recorder([(status, {"cancelled": 1, "errors": [{"orderId": 5, "error": "x"}, {"orderId": 6, "error": "y"}]}, None)])
    venue, _ = make_venue(rec)
    r = run(venue.cancel_all(TID))
    assert r.remaining == 2 and not r.all_cancelled


def test_cancel_single_treats_already_closed_as_done():
    rec = Recorder([(409, err("CONFLICT", "already closed"), None)])
    venue, _ = make_venue(rec)
    run(venue.cancel("1001", TID))
    assert rec.requests[0].method == "DELETE" and rec.requests[0].url.path.endswith("/orders/1001")


# --- reads ------------------------------------------------------------------


def test_get_open_orders_paginates_and_maps():
    page1 = {"data": [{"id": 1, "exchangeId": "1077", "side": "yes", "action": "buy", "quantity": 10,
                       "priceLimit": 0.3, "open": True, "createdAt": "2026-10-01T17:00:00Z",
                       "expirationDate": "2026-10-01T17:00:30Z"}],
             "pagination": {"limit": 200, "hasMore": True, "nextCursor": "c1"}}  # fmt: skip
    page2 = {"data": [{"id": 2, "exchangeId": "1076", "side": "no", "action": "buy", "quantity": 5,
                       "priceLimit": 0.6, "open": True, "createdAt": "2026-10-01T17:00:00Z", "expirationDate": None}],
             "pagination": {"limit": 200, "hasMore": False, "nextCursor": None}}  # fmt: skip
    rec = Recorder([(200, page1, None), (200, page2, None)])
    venue, _ = make_venue(rec)
    orders = run(venue.get_open_orders(TID))
    params = [dict(r.url.params) for r in rec.non_tournament_requests()]
    assert params[0] == {"status": "open", "tournamentId": TID, "limit": "200"}
    assert params[1]["cursor"] == "c1"
    assert [(o.id, o.exchange_id, o.price, o.status) for o in orders] == [("1", "1077", 0.3, OrderStatus.OPEN), ("2", "1076", 0.6, OrderStatus.OPEN)]


def test_get_positions_uses_tournament_portfolio_path():
    body = {"positions": [{"exchangeId": "1077", "marketId": "388", "marketTitle": "t", "option": "YES", "settled": False,
                           "quantity": -20, "avgCost": 0.4, "currentPrice": 0.35, "marketValue": 0, "costBasis": 0,
                           "unrealizedPnl": 0, "unrealizedPnlPct": 0, "moneyEarned": 0, "lots": []}],
            "summary": {}}  # fmt: skip
    rec = Recorder([(200, body, None)])
    venue, _ = make_venue(rec)
    pos = run(venue.get_positions(TID))
    assert rec.non_tournament_requests()[0].url.path == f"/api/v1/tournaments/{SLUG}/portfolio/positions"
    assert [(p.exchange_id, p.quantity, p.avg_cost) for p in pos] == [("1077", -20, 0.4)]


def test_get_positions_refuses_other_tournament():
    venue, _ = make_venue(Recorder([]))
    with pytest.raises(ValueError, match="tournament"):
        run(venue.get_positions("another-tournament"))


def test_get_balance_reads_my_balance():
    venue, _ = make_venue(Recorder([]))
    assert run(venue.get_balance(TID)) == 98765.5


def test_get_book_passes_tournament():
    book = {"exchangeId": "1077", "marketId": "388", "depth": 5, "bestBid": 0.4, "bestAsk": 0.45, "spread": 0.05,
            "bids": [{"price": 0.4, "quantity": 100}], "asks": [{"price": 0.45, "quantity": 50}]}  # fmt: skip
    rec = Recorder([(200, book, None)])
    venue, _ = make_venue(rec)
    b = run(venue.get_book("1077", TID))
    assert dict(rec.requests[-1].url.params)["tournamentId"] == TID
    assert b.bids[0].price == 0.4 and b.asks[0].quantity == 50


def test_reads_retry_429_and_report():
    seen = []
    rec = Recorder([(429, err("RATE_LIMITED"), None), (200, {"cancelled": 0, "errors": []}, None)])
    venue, _ = make_venue(rec, rate_limited=lambda e, ra: seen.append((e, ra)))
    run(venue.cancel_all(TID))
    assert seen == [("POST /orders/cancel-all", None)]


def test_get_top_of_books_bulk_read_chunks_at_100():
    def page(ids):
        return {"data": [{"exchangeId": i, "marketId": "m", "option": "YES", "latestPrice": None,
                          "bestBid": 0.4, "bestAsk": None, "spread": None} for i in ids], "missingIds": []}  # fmt: skip

    ids = [str(i) for i in range(150)]
    rec = Recorder([(200, page(ids[:100]), None), (200, page(ids[100:]), None)])
    venue, _ = make_venue(rec)
    out = run(venue.get_top_of_books(ids, TID))
    params = [dict(r.url.params) for r in rec.requests]
    assert [len(p["ids"].split(",")) for p in params] == [100, 50]
    assert all(p["tournamentId"] == TID for p in params)
    assert out["149"] == (0.4, None) and len(out) == 150


# --- fills (reconciliation input) ---------------------------------------------


def _fill(i, qty, side=None, price=0.5):
    return {"id": i, "orderId": 100 + i, "exchangeId": "1077", "marketId": "388", "price": price,
            "quantity": qty, "side": side or ("yes" if qty > 0 else "no"), "filledAt": "2026-10-01T17:00:00Z"}  # fmt: skip


def test_get_new_fills_pages_until_a_known_fill_and_returns_oldest_first():
    page1 = {"data": [_fill(5, 10), _fill(4, -3)], "pagination": {"limit": 200, "hasMore": True, "nextCursor": "c"}}
    page2 = {"data": [_fill(3, 7), _fill(2, 1)], "pagination": {"limit": 200, "hasMore": True, "nextCursor": "d"}}
    rec = Recorder([(200, page1, None), (200, page2, None)])
    venue, _ = make_venue(rec)
    fills = run(venue.get_new_fills(TID, known_ids={"2", "1"}))
    assert rec.requests[0].url.path == f"/api/v1/tournaments/{SLUG}/portfolio/fills"
    assert len(rec.requests) == 2  # stopped at the page holding known fill 2
    assert [f.id for f in fills] == ["3", "4", "5"]
    # Outcome-signed quantity -> signed YES-position change (TODO(api) reading).
    assert [(f.side, f.action, f.quantity) for f in fills] == [("yes", "buy", 7), ("no", "buy", 3), ("yes", "buy", 10)]


def test_get_new_fills_refuses_fractional_quantity():
    rec = Recorder([(200, {"data": [_fill(1, 2.5)], "pagination": {"limit": 200, "hasMore": False, "nextCursor": None}}, None)])
    venue, _ = make_venue(rec)
    with pytest.raises(ValueError, match="fractional"):
        run(venue.get_new_fills(TID, known_ids=set()))
