"""Polymarket read-only market data (CLAUDE.md Hard Rule 5: market data only,
no orders, ever). Built from the saved specs in docs/polymarket/
(gamma-openapi.yaml, clob-openapi.yaml) and the market-data guides there;
see docs/polymarket/README.md for the facts relied on.

Two public, unauthenticated APIs:
  Gamma  GET /markets?clob_token_ids=...   market metadata, outcomes, state
  CLOB   GET /book?token_id=...            one outcome token's order book

Outcome tokens: Gamma's `outcomes`, `outcomePrices` and `clobTokenIds` are
JSON-encoded arrays correlated by index, index 0 = YES, 1 = NO. We accept a
market only when its labels are exactly ["Yes", "No"], so a reordered or
non-binary market fails loudly instead of flipping a price.

Prices are dollars per share and a share pays $1, so they are already
probabilities in [0, 1]; anything outside fails loudly (a cents price must
never become a fair value). Token ids stay strings (77 digits).

This class deliberately only ever issues GET requests.
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime

import httpx
from pydantic import BaseModel

# docs/polymarket/gamma-openapi.yaml and clob-openapi.yaml `servers`.
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"
MAX_RETRIES = 5


class PolymarketSchemaError(ValueError):
    """A response that doesn't match what docs/polymarket/ says."""


class PolymarketMarket(BaseModel):
    """One binary Gamma market (YES/NO outcome pair)."""

    id: str
    question: str
    condition_id: str
    slug: str  # identification only; never used to infer party
    yes_token_id: str
    no_token_id: str
    yes_price: float | None  # outcomePrices[0]
    no_price: float | None
    active: bool | None
    closed: bool | None
    accepting_orders: bool | None
    enable_order_book: bool | None
    neg_risk: bool | None
    best_bid: float | None
    best_ask: float | None
    last_trade_price: float | None
    liquidity: float | None  # liquidityNum; TODO(api): units not documented
    volume: float | None  # volumeNum; TODO(api): units not documented
    end_date: datetime | None
    tick_size: float | None
    event_titles: list[str]

    @property
    def trade_ready(self) -> bool:
        """market-data_market-details.md: active and not closed and
        acceptingOrders."""
        return bool(self.active) and not self.closed and bool(self.accepting_orders)


class PolymarketBook(BaseModel):
    """CLOB OrderBookSummary for one outcome token, best levels only."""

    token_id: str
    condition_id: str
    best_bid: float | None
    best_bid_size: float | None  # shares
    best_ask: float | None
    best_ask_size: float | None
    tick_size: float | None
    neg_risk: bool | None
    last_trade_price: float | None
    timestamp: str  # TODO(api): units undocumented; use our own fetch time
    hash: str

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


def _json_array(value, field: str) -> list:
    """Gamma encodes arrays as JSON strings (spec: type string). Accept a
    decoded list too; anything else is a schema error."""
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        raise PolymarketSchemaError(f"{field}: expected JSON array string, got {value!r}")
    try:
        out = json.loads(value)
    except ValueError as e:
        raise PolymarketSchemaError(f"{field}: not JSON: {value!r}") from e
    if not isinstance(out, list):
        raise PolymarketSchemaError(f"{field}: not an array: {value!r}")
    return out


def _price(value, field: str) -> float | None:
    if value is None or value == "":
        return None
    p = float(value)
    if not 0.0 <= p <= 1.0:
        raise PolymarketSchemaError(f"{field}={value!r} is not a price in [0, 1]")
    return p


def _float(value) -> float | None:
    return None if value is None or value == "" else float(value)


def _datetime(value) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def parse_gamma_market(raw: dict) -> PolymarketMarket:
    """Gamma `Market` -> PolymarketMarket. Raises PolymarketSchemaError
    unless outcomes are exactly ["Yes", "No"] with two distinct token ids."""
    outcomes = _json_array(raw.get("outcomes"), "outcomes")
    if outcomes != ["Yes", "No"]:
        raise PolymarketSchemaError(f"market {raw.get('id')}: outcomes {outcomes!r} are not ['Yes', 'No']")
    tokens = _json_array(raw.get("clobTokenIds"), "clobTokenIds")
    if len(tokens) != 2 or not all(isinstance(t, str) and t for t in tokens) or tokens[0] == tokens[1]:
        raise PolymarketSchemaError(f"market {raw.get('id')}: clobTokenIds {tokens!r} are not two distinct ids")
    prices = _json_array(raw["outcomePrices"], "outcomePrices") if raw.get("outcomePrices") else [None, None]
    if len(prices) != 2:
        raise PolymarketSchemaError(f"market {raw.get('id')}: outcomePrices {prices!r}")
    return PolymarketMarket(
        id=str(raw["id"]),
        question=raw.get("question") or "",
        condition_id=raw.get("conditionId") or "",
        slug=raw.get("slug") or "",
        yes_token_id=tokens[0],
        no_token_id=tokens[1],
        yes_price=_price(prices[0], "outcomePrices[0]"),
        no_price=_price(prices[1], "outcomePrices[1]"),
        active=raw.get("active"),
        closed=raw.get("closed"),
        accepting_orders=raw.get("acceptingOrders"),
        enable_order_book=raw.get("enableOrderBook"),
        neg_risk=raw.get("negRisk"),
        best_bid=_price(raw.get("bestBid"), "bestBid"),
        best_ask=_price(raw.get("bestAsk"), "bestAsk"),
        last_trade_price=_price(raw.get("lastTradePrice"), "lastTradePrice"),
        liquidity=_float(raw.get("liquidityNum")),
        volume=_float(raw.get("volumeNum")),
        end_date=_datetime(raw.get("endDate")),
        tick_size=_float(raw.get("orderPriceMinTickSize")),
        event_titles=[e.get("title") or "" for e in raw.get("events") or []],
    )


def _best(levels: list[dict], side: str) -> tuple[float | None, float | None]:
    """(price, size) of the best level. The spec and the prose guide
    disagree on sort order, so take max bid / min ask; ignore empty levels."""
    parsed = [(_price(lv["price"], f"{side}.price"), float(lv["size"])) for lv in levels]
    parsed = [(p, s) for p, s in parsed if p is not None and s > 0]
    if not parsed:
        return None, None
    return (max if side == "bids" else min)(parsed, key=lambda ps: ps[0])


def parse_book(raw: dict) -> PolymarketBook:
    """CLOB `OrderBookSummary` -> PolymarketBook."""
    bid, bid_size = _best(raw.get("bids") or [], "bids")
    ask, ask_size = _best(raw.get("asks") or [], "asks")
    return PolymarketBook(
        token_id=raw["asset_id"],
        condition_id=raw.get("market") or "",
        best_bid=bid,
        best_bid_size=bid_size,
        best_ask=ask,
        best_ask_size=ask_size,
        tick_size=_float(raw.get("tick_size")),
        neg_risk=raw.get("neg_risk"),
        last_trade_price=_price(raw.get("last_trade_price"), "last_trade_price"),
        timestamp=str(raw.get("timestamp") or ""),
        hash=raw.get("hash") or "",
    )


class PolymarketReadOnly:
    """GET-only client for public Gamma and CLOB market data. Rate limits
    (docs/polymarket/rate_limits.md) are per IP and generous (Gamma
    /markets 300 per 10 s, CLOB /books 500 per 10 s); we pace every
    request anyway and back off on 429."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        gamma_base_url: str = GAMMA_BASE_URL,
        clob_base_url: str = CLOB_BASE_URL,
        request_delay_seconds: float = 0.25,
    ) -> None:
        self._client = client
        self._gamma = gamma_base_url.rstrip("/")
        self._clob = clob_base_url.rstrip("/")
        self._delay = request_delay_seconds
        self._backoff_base = 1.0

    async def _get(self, url: str, params=None) -> httpx.Response | None:
        """JSON GET; None on 404. Retries 429 and 503 only, with backoff.
        TODO(api): the docs say over-limit requests are throttled (queued)
        by Cloudflare rather than rejected and don't document a 429 body or
        Retry-After; 429 handling is defensive."""
        resp = None
        for attempt in range(MAX_RETRIES):
            resp = await self._client.get(url, params=params)
            await asyncio.sleep(self._delay)
            if resp.status_code in (429, 503):
                await asyncio.sleep(self._backoff_base * (2**attempt + random.uniform(0, 1)))
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp
        assert resp is not None
        resp.raise_for_status()
        return resp

    async def get_markets_by_token(self, token_ids: list[str], chunk_size: int = 20) -> dict[str, PolymarketMarket]:
        """Gamma GET /markets?clob_token_ids=..&clob_token_ids=.. (array
        query parameter), keyed by the token id asked for (YES or NO).
        `closed` defaults to false in the spec, so a closed market is
        simply absent. TODO(api): no documented maximum number of ids per
        request; 20 is our own conservative chunk."""
        out: dict[str, PolymarketMarket] = {}
        for i in range(0, len(token_ids), chunk_size):
            chunk = token_ids[i : i + chunk_size]
            resp = await self._get(f"{self._gamma}/markets", [("clob_token_ids", t) for t in chunk])
            for raw in resp.json() if resp is not None else []:
                m = parse_gamma_market(raw)
                for t in chunk:
                    if t in (m.yes_token_id, m.no_token_id):
                        out[t] = m
        return out

    async def get_book(self, token_id: str) -> PolymarketBook | None:
        """CLOB GET /book; None when no order book exists (404)."""
        resp = await self._get(f"{self._clob}/book", {"token_id": token_id})
        return parse_book(resp.json()) if resp is not None else None

    async def get_books(self, token_ids: list[str]) -> dict[str, PolymarketBook]:
        """One GET /book per token; tokens with no book are absent.
        TODO(api): clob-openapi.yaml documents GET /books?token_ids=a,b, but
        it answered 400 live on 2026-10-01 (literal or encoded comma). The
        documented POST /books works; we stay GET-only and use /book
        (1500 per 10 s limit, far above our ~120 per poll)."""
        out: dict[str, PolymarketBook] = {}
        for t in token_ids:
            b = await self.get_book(t)
            if b is not None:
                out[t] = b
        return out
