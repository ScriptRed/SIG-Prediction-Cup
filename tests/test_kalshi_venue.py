"""predcup/venues/kalshi.py batch read: GET /markets?tickers=... (documented
in docs/kalshi/openapi.yaml), GET-only."""

from __future__ import annotations

import asyncio

import httpx

from predcup.venues.kalshi import KalshiReadOnly


def _m(t: str) -> dict:
    return {"ticker": t, "event_ticker": t.rsplit("-", 1)[0], "title": "", "subtitle": "", "yes_sub_title": "",
            "no_sub_title": "", "status": "active", "yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4200",
            "volume_fp": "10.00", "rules_primary": "", "rules_secondary": ""}  # fmt: skip


def test_get_markets_batches_tickers_and_chunks():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path.endswith("/markets")
        params = dict(request.url.params)
        seen.append(params)
        tickers = params["tickers"].split(",")
        return httpx.Response(200, json={"markets": [_m(t) for t in tickers], "cursor": ""})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            k = KalshiReadOnly(c, "https://k.test/trade-api/v2", request_delay_seconds=0)
            return await k.get_markets([f"T-{i}" for i in range(120)], chunk_size=50)

    out = asyncio.run(go())
    assert [len(p["tickers"].split(",")) for p in seen] == [50, 50, 20]
    assert all(p["limit"] == str(len(p["tickers"].split(","))) for p in seen)
    assert sorted(out) == sorted(f"T-{i}" for i in range(120))
    assert out["T-7"].yes_bid == 0.4


def test_get_markets_empty_list_makes_no_request():
    def handler(request):  # pragma: no cover
        raise AssertionError("no request expected")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await KalshiReadOnly(c, "https://k.test", 0).get_markets([])

    assert asyncio.run(go()) == {}
