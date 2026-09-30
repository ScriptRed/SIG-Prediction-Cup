"""Kalshi read-only market data (CLAUDE.md Hard Rule 5: market data only,
no orders, ever). Built from the saved spec in docs/kalshi/openapi.yaml
(Kalshi Trade API 3.32.0) and docs/kalshi/quick_start_market_data.md.

Minimal slice for now: GET /markets/{ticker} and GET /events/{event_ticker},
both unauthenticated per the quick start. The full polling adapter that
implements `venues.base.Venue` (PLAN step 3) builds on this module. This
class deliberately only ever issues GET requests.
"""

from __future__ import annotations

import asyncio
import random

import httpx
from pydantic import BaseModel

# docs/kalshi/openapi.yaml `servers`: production Trade API server.
DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
MAX_RETRIES = 5


class KalshiNotFound(Exception):
    pass


class KalshiMarket(BaseModel):
    """One Kalshi binary market. Prices are YES-side floats in [0, 1]
    (converted from the spec's FixedPointDollars strings; one contract
    pays $1, per `notional_value_dollars`)."""

    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    yes_sub_title: str
    no_sub_title: str
    status: str
    close_time: str | None
    expected_expiration_time: str | None
    latest_expiration_time: str | None
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None
    volume: float
    volume_24h: float
    rules_primary: str
    rules_secondary: str
    custom_strike: dict | None

    @property
    def mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def spread(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid


class KalshiEvent(BaseModel):
    event_ticker: str
    series_ticker: str
    title: str
    sub_title: str
    strike_date: str | None
    settlement_sources: list[str]  # "name (url)"


def _dollars(value: str | None) -> float | None:
    """FixedPointDollars string (e.g. "0.5600") -> float."""
    if value is None or value == "":
        return None
    return float(value)


def parse_market(raw: dict) -> KalshiMarket:
    """`GetMarketResponse.market` -> KalshiMarket."""
    bid = _dollars(raw.get("yes_bid_dollars"))
    ask = _dollars(raw.get("yes_ask_dollars"))
    # TODO(api): the spec doesn't say how an empty side is reported. Treat a
    # bid of 0 or an ask of 1 as "no order on that side" - the conservative
    # reading (a missing side shows as no two-sided book, never a fake mid).
    if bid is not None and bid <= 0:
        bid = None
    if ask is not None and ask >= 1:
        ask = None
    return KalshiMarket(
        ticker=raw["ticker"],
        event_ticker=raw["event_ticker"],
        title=raw.get("title") or "",
        subtitle=raw.get("subtitle") or "",
        yes_sub_title=raw.get("yes_sub_title") or "",
        no_sub_title=raw.get("no_sub_title") or "",
        status=raw.get("status") or "",
        close_time=raw.get("close_time"),
        expected_expiration_time=raw.get("expected_expiration_time"),
        latest_expiration_time=raw.get("latest_expiration_time"),
        yes_bid=bid,
        yes_ask=ask,
        last_price=_dollars(raw.get("last_price_dollars")),
        volume=float(raw.get("volume_fp") or 0),
        volume_24h=float(raw.get("volume_24h_fp") or 0),
        rules_primary=raw.get("rules_primary") or "",
        rules_secondary=raw.get("rules_secondary") or "",
        custom_strike=raw.get("custom_strike") or None,
    )


def parse_event(raw: dict) -> KalshiEvent:
    """`GetEventResponse.event` -> KalshiEvent."""
    sources = [
        f"{s.get('name', '')} ({s.get('url', '')})".strip()
        for s in (raw.get("settlement_sources") or [])
    ]
    return KalshiEvent(
        event_ticker=raw["event_ticker"],
        series_ticker=raw.get("series_ticker") or "",
        title=raw.get("title") or "",
        sub_title=raw.get("sub_title") or "",
        strike_date=raw.get("strike_date"),
        settlement_sources=sources,
    )


class KalshiReadOnly:
    """GET-only client. No auth: these are the public market data
    endpoints (docs/kalshi/quick_start_market_data.md)."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str = DEFAULT_BASE_URL,
        request_delay_seconds: float = 0.25,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        # TODO(api): docs/kalshi/rate_limits.md only gives budgets for
        # authenticated tiers; the unauthenticated limit isn't stated. Pace
        # every request and back off on 429 (no Retry-After is sent).
        self._delay = request_delay_seconds

    async def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(MAX_RETRIES):
            resp = await self._client.get(f"{self._base_url}{path}", params=params)
            await asyncio.sleep(self._delay)
            if resp.status_code == 429:
                await asyncio.sleep(2**attempt + random.uniform(0, 1))
                continue
            if resp.status_code == 404:
                raise KalshiNotFound(path)
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    async def get_market(self, ticker: str) -> KalshiMarket:
        data = await self._get(f"/markets/{ticker}")
        return parse_market(data["market"])

    async def get_event(self, event_ticker: str) -> KalshiEvent:
        data = await self._get(f"/events/{event_ticker}")
        return parse_event(data["event"])

    async def get_markets(self, tickers: list[str], chunk_size: int = 50) -> dict[str, KalshiMarket]:
        """Many markets per request: GET /markets?tickers=a,b,c. The spec
        documents `tickers` but no maximum count.
        TODO(api): chunk size 50 is our own conservative choice."""
        out: dict[str, KalshiMarket] = {}
        for i in range(0, len(tickers), chunk_size):
            chunk = tickers[i : i + chunk_size]
            data = await self._get("/markets", {"tickers": ",".join(chunk), "limit": len(chunk)})
            for raw in data.get("markets") or []:
                m = parse_market(raw)
                out[m.ticker] = m
        return out
