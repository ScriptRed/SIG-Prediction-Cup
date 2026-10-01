"""venues/polymarket.py: read-only Gamma + CLOB, parsed strictly from
docs/polymarket/. Outcome tokens are correlated by index with YES at 0
(market-data_market-details.md) and the labels must say so; prices are
dollars per $1 share, i.e. already probabilities in [0, 1]."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from predcup.venues.polymarket import (
    PolymarketReadOnly,
    PolymarketSchemaError,
    parse_book,
    parse_gamma_market,
)

YES = "107505882767731489358349912513945399560393482969656700824895970500493757150417"
NO = "7305630249804085635496399869905769372294302716159034447326228509068694952392"


def gamma_raw(**over) -> dict:
    """A Gamma Market as the docs show it: arrays JSON-encoded in strings."""
    raw = {
        "id": "3344136",
        "question": "Will the Republicans win the Texas Senate race in 2026?",
        "conditionId": "0xabc",
        "slug": "will-the-republicans-win-the-texas-senate-race-in-2026",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.085", "0.915"]',
        "clobTokenIds": json.dumps([YES, NO]),
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "negRisk": True,
        "bestBid": 0.08,
        "bestAsk": 0.09,
        "lastTradePrice": 0.085,
        "liquidityNum": 17885.05,
        "volumeNum": 217.85,
        "endDate": "2026-11-04T04:59:00Z",
        "orderPriceMinTickSize": 0.001,
        "groupItemTitle": "Ken Paxton (R)",
        "events": [{"id": "57672", "title": "Texas Senate Election Winner", "slug": "texas-senate-election-winner"}],
    }
    raw.update(over)
    return raw


def book_raw(**over) -> dict:
    """CLOB OrderBookSummary (clob-openapi.yaml), prices/sizes as strings."""
    raw = {
        "market": "0xabc",
        "asset_id": YES,
        "timestamp": "1234567890",
        "hash": "a1b2",
        "bids": [{"price": "0.44", "size": "200"}, {"price": "0.45", "size": "100"}],
        "asks": [{"price": "0.47", "size": "250"}, {"price": "0.46", "size": "150"}],
        "min_order_size": "1",
        "tick_size": "0.01",
        "neg_risk": False,
        "last_trade_price": "0.45",
    }
    raw.update(over)
    return raw


# --- outcome-token ordering ------------------------------------------------


def test_yes_token_is_index_0_and_no_token_index_1():
    m = parse_gamma_market(gamma_raw())
    assert m.yes_token_id == YES and m.no_token_id == NO


def test_yes_price_is_the_yes_index_of_outcome_prices():
    m = parse_gamma_market(gamma_raw())
    assert m.yes_price == pytest.approx(0.085) and m.no_price == pytest.approx(0.915)


def test_token_ids_stay_strings_not_floats():
    # 77-digit ids would lose precision as JSON numbers / floats
    m = parse_gamma_market(gamma_raw())
    assert isinstance(m.yes_token_id, str) and len(m.yes_token_id) == len(YES)


@pytest.mark.parametrize(
    "outcomes",
    ['["No", "Yes"]', '["Republican", "Democrat"]', '["Yes"]', '["Yes", "No", "Other"]', '["yes", "no"]'],
)
def test_anything_but_yes_no_in_that_order_is_refused(outcomes):
    with pytest.raises(PolymarketSchemaError):
        parse_gamma_market(gamma_raw(outcomes=outcomes))


@pytest.mark.parametrize("ids", ["[]", json.dumps([YES]), json.dumps([YES, NO, "3"]), json.dumps([YES, YES]), "not json", None])
def test_token_id_array_must_be_two_distinct_ids(ids):
    with pytest.raises(PolymarketSchemaError):
        parse_gamma_market(gamma_raw(clobTokenIds=ids))


def test_already_decoded_arrays_are_accepted():
    # the spec says string; accept a real list too rather than mis-parse it
    m = parse_gamma_market(gamma_raw(outcomes=["Yes", "No"], clobTokenIds=[YES, NO], outcomePrices=["0.1", "0.9"]))
    assert m.yes_token_id == YES and m.yes_price == pytest.approx(0.1)


# --- price units -------------------------------------------------------------


def test_gamma_prices_are_probabilities():
    m = parse_gamma_market(gamma_raw())
    assert m.best_bid == pytest.approx(0.08) and m.best_ask == pytest.approx(0.09)
    assert m.last_trade_price == pytest.approx(0.085)


@pytest.mark.parametrize("field,value", [("bestBid", 8.0), ("bestAsk", 9), ("lastTradePrice", -0.1)])
def test_gamma_price_outside_unit_interval_is_refused(field, value):
    # a cents-denominated price (8 for $0.08) must fail loudly, not become a fair value
    with pytest.raises(PolymarketSchemaError):
        parse_gamma_market(gamma_raw(**{field: value}))


def test_outcome_price_outside_unit_interval_is_refused():
    with pytest.raises(PolymarketSchemaError):
        parse_gamma_market(gamma_raw(outcomePrices='["8.5", "91.5"]'))


def test_book_prices_are_probabilities_and_sizes_shares():
    b = parse_book(book_raw())
    assert b.best_bid == pytest.approx(0.45) and b.best_ask == pytest.approx(0.46)
    assert b.best_bid_size == pytest.approx(100) and b.best_ask_size == pytest.approx(150)
    assert b.mid == pytest.approx(0.455) and b.spread == pytest.approx(0.01)


def test_book_price_outside_unit_interval_is_refused():
    with pytest.raises(PolymarketSchemaError):
        parse_book(book_raw(bids=[{"price": "45", "size": "1"}]))


# --- order books ---------------------------------------------------------------


@pytest.mark.parametrize("reverse", [False, True])
def test_best_prices_do_not_depend_on_level_order(reverse):
    # clob-openapi.yaml and the prose guide disagree on sort order
    raw = book_raw()
    if reverse:
        raw["bids"], raw["asks"] = raw["bids"][::-1], raw["asks"][::-1]
    b = parse_book(raw)
    assert (b.best_bid, b.best_ask) == (pytest.approx(0.45), pytest.approx(0.46))


def test_empty_side_has_no_best_price_and_no_mid():
    b = parse_book(book_raw(asks=[]))
    assert b.best_ask is None and b.mid is None and b.spread is None


def test_zero_size_levels_are_ignored():
    b = parse_book(book_raw(bids=[{"price": "0.50", "size": "0"}, {"price": "0.45", "size": "100"}]))
    assert b.best_bid == pytest.approx(0.45)


# --- the client: GET only ------------------------------------------------------


def _client(handler) -> PolymarketReadOnly:
    seen: list[httpx.Request] = []

    def h(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    c = PolymarketReadOnly(httpx.AsyncClient(transport=httpx.MockTransport(h)), request_delay_seconds=0)
    c.seen = seen  # type: ignore[attr-defined]
    return c


def test_get_markets_by_token_queries_gamma_with_repeated_clob_token_ids():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "gamma-api.polymarket.com" and req.url.path == "/markets"
        assert req.url.params.get_list("clob_token_ids") == [YES]
        return httpx.Response(200, json=[gamma_raw()])

    c = _client(handler)
    out = asyncio.run(c.get_markets_by_token([YES]))
    assert set(out) == {YES} and out[YES].question.startswith("Will the Republicans")


def test_market_found_by_its_no_token_is_still_keyed_by_the_asked_token():
    c = _client(lambda req: httpx.Response(200, json=[gamma_raw()]))
    out = asyncio.run(c.get_markets_by_token([NO]))
    assert out[NO].yes_token_id == YES


def test_get_books_is_one_get_book_per_token_and_skips_missing_books():
    # GET /books?token_ids=a,b answered 400 live on 2026-10-01; POST /books
    # works but the client stays GET-only, so one documented GET /book each
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "clob.polymarket.com" and req.url.path == "/book"
        tid = req.url.params["token_id"]
        if tid == "404":
            return httpx.Response(404, json={"error": "No orderbook exists for the requested token id"})
        return httpx.Response(200, json=book_raw(asset_id=tid))

    c = _client(handler)
    out = asyncio.run(c.get_books([YES, NO, "404"]))
    assert set(out) == {YES, NO}


def test_get_book_404_means_no_book():
    c = _client(lambda req: httpx.Response(404, json={"error": "No orderbook exists for the requested token id"}))
    assert asyncio.run(c.get_book(YES)) is None


def test_429_is_retried():
    calls = iter([httpx.Response(429), httpx.Response(200, json=book_raw())])
    c = _client(lambda req: next(calls))
    c._backoff_base = 0  # type: ignore[attr-defined]
    assert asyncio.run(c.get_book(YES)).best_bid == pytest.approx(0.45)


def test_client_only_ever_sends_get():
    c = _client(lambda req: httpx.Response(200, json=[gamma_raw()] if "gamma" in req.url.host else book_raw()))
    asyncio.run(c.get_markets_by_token([YES]))
    asyncio.run(c.get_books([YES]))
    assert {r.method for r in c.seen} == {"GET"}  # type: ignore[attr-defined]


def test_read_only_client_has_no_order_methods():
    for name in ("place_order", "place_batch", "cancel", "cancel_all"):
        assert not hasattr(PolymarketReadOnly, name)
