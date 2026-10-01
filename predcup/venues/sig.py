"""The competition platform (Super Market) adapter. Source of truth for
every shape here: docs/platform/openapi.json (summary in
docs/platform/SUMMARY.md).

CLAUDE.md rules implemented here:
- Every read and order passes the Cup's tournamentId; the adapter is bound
  to one tournament slug and refuses any other tournament_id.
- Positions come from /tournaments/{slug}/portfolio/positions, never the
  no-argument /portfolio reads.
- Limit prices are rounded to the 0.005 tick within [0.005, 0.995] here.
- Retries: only 429, 503 and 409 REQUEST_IN_FLIGHT, exponential backoff +
  jitter (Retry-After honoured), same idempotency key and identical payload.
  502 ORDER_STATUS_UNKNOWN is retried with the same key (the engine replays
  a resolved order, never double-places); if it persists, OrderStatusUnknown
  is raised so the caller reconciles. Never a new key for the same order.
- Every 429 is reported through `on_rate_limited` (RiskManager.record_rate_limited).
- Other 4xx are never retried: SigOrderRejected, for risk.record_order_rejection.

Hard rule 1: nothing outside the order router may call place_order or
place_batch (enforced by tests/test_order_path.py once the router exists).
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from dataclasses import dataclass

import httpx

from predcup.models import (
    MAX_PRICE,
    MIN_PRICE,
    TICK,
    Fill,
    Market,
    Order,
    OrderBook,
    OrderBookLevel,
    OrderStatus,
    Position,
)
from predcup.venues.base import BatchItemResult, CancelAllResult, Venue

MAX_BATCH = 50  # BatchOrderInput.orders maxItems
PAGE_LIMIT_ORDERS = 200  # GET /orders limit max
PAGE_LIMIT_MARKETS = 100  # GET /markets limit max


class SigApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


class SigOrderRejected(SigApiError):
    """A non-retryable 4xx on an order endpoint. The caller halts that
    market via RiskManager.record_order_rejection; never retried."""


class OrderStatusUnknown(Exception):
    """502 ORDER_STATUS_UNKNOWN persisted through retries. The order (or
    batch) may have gone through: reconcile, or retry with the SAME key."""

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(f"order status unknown for idempotency key {idempotency_key}")
        self.idempotency_key = idempotency_key


def new_idempotency_key() -> str:
    """A fresh key per logical order or batch. Retries reuse it."""
    return str(uuid.uuid4())


def format_price(price: float) -> float:
    """Round a limit price to the 0.005 tick; refuse anything outside
    [0.005, 0.995] after rounding."""
    ticks = round(price / TICK)
    p = round(ticks * TICK, 3)
    if p < MIN_PRICE - 1e-9 or p > MAX_PRICE + 1e-9:
        raise ValueError(f"limit price {price} outside [{MIN_PRICE}, {MAX_PRICE}]")
    return p


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _retry_after_seconds(raw: str | None) -> float | None:
    """Retry-After as a non-negative number of seconds, else None (the spec
    doesn't fix the format; HTTP also allows a date). None -> our own
    exponential backoff, never an exception mid-request."""
    try:
        value = float(raw) if raw else None
    except ValueError:
        return None
    return value if value is not None and value >= 0 else None


def _error_of(body: Any) -> tuple[str, str, dict]:
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        e = body["error"]
        return e.get("code", ""), e.get("message", ""), e.get("details") or {}
    return "", "", {}


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


RateLimitCallback = Callable[[str, "float | None"], None]

PNL_PERIODS = ("day", "week", "month", "quarter", "year", "all")


@dataclass(frozen=True)
class TournamentPnl:
    """GET /tournaments/{slug}/portfolio/pnl (Cup tournament only)."""

    period: str
    period_pnl: float | None  # null when the API can't compute it
    unrealized_pnl: float
    total_account_value: float
    roi: float | None


class SigVenue(Venue):
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str,
        tournament_slug: str,
        on_rate_limited: RateLimitCallback | None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = 5,
        backoff_base_seconds: float = 0.5,
        backoff_cap_seconds: float = 30.0,
    ) -> None:
        # Fail closed: 429s must reach the size ramp.
        if on_rate_limited is None:
            raise ValueError("on_rate_limited is required (RiskManager.record_rate_limited)")
        if not tournament_slug:
            raise ValueError("tournament_slug is required")
        self._client = client
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._slug = tournament_slug
        self._on_rate_limited = on_rate_limited
        self._sleep = sleep
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds
        self._tournament_id: str | None = None

    # --- plumbing -------------------------------------------------------------

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return retry_after
        return min(self._backoff_cap, self._backoff_base * 2**attempt) + random.uniform(0, self._backoff_base)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        label: str,
        params: dict | None = None,
        body: dict | None = None,
        classify: Callable[[httpx.Response, Any], str] | None = None,
        unknown_key: str | None = None,
        rejection: type[SigApiError] = SigApiError,
    ) -> tuple[httpx.Response, Any]:
        """One logical request with CLAUDE.md's retry rules. The same
        `body` object is re-sent on every attempt (same key, same payload).
        `classify` may mark a response "retry"/"retry_rate_limited" (e.g. a
        207 batch with transient items)."""
        resp: httpx.Response | None = None
        data: Any = None
        for attempt in range(self._max_retries + 1):
            resp = await self._client.request(method, f"{self._base}{path}", headers=self._headers,
                                              params=params, json=body)  # fmt: skip
            data = _json(resp)
            code, message, details = _error_of(data)
            retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
            status = resp.status_code

            verdict = classify(resp, data) if classify else None
            if verdict is None:
                if status == 429:
                    verdict = "retry_rate_limited"
                elif status == 503 or (status == 409 and code == "REQUEST_IN_FLIGHT"):
                    verdict = "retry"
                elif status == 502 and unknown_key is not None:
                    verdict = "unknown"
                elif status >= 400:
                    raise rejection(status, code or str(status), message or resp.text[:200], details)
                else:
                    verdict = "done"

            if verdict == "done":
                return resp, data
            if verdict == "retry_rate_limited":
                self._on_rate_limited(label, retry_after)
            if attempt < self._max_retries:
                await self._sleep(self._backoff(attempt, retry_after))
                continue
            if verdict == "unknown":
                raise OrderStatusUnknown(unknown_key or "")
            if classify is not None and status < 400:
                return resp, data  # retries exhausted on a partial result: report it as is
            raise SigApiError(status, code or str(status), message or "retries exhausted", details)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _get(self, path: str, params: dict | None = None) -> Any:
        _, data = await self._send("GET", path, label=f"GET {path}", params=params)
        return data

    async def tournament_id(self) -> str:
        if self._tournament_id is None:
            data = await self._get(f"/tournaments/{self._slug}")
            self._tournament_id = data["id"]
        return self._tournament_id

    async def tournament_summary(self) -> dict:
        """GET /tournaments/{slug} (TournamentSummary): id, status,
        startDate, endDate, myBalance, ..."""
        data = await self._get(f"/tournaments/{self._slug}")
        self._tournament_id = data["id"]
        return data

    async def _check(self, tournament_id: str) -> str:
        tid = await self.tournament_id()
        if tournament_id != tid:
            raise ValueError(f"tournament {tournament_id!r} is not this adapter's Cup tournament ({self._slug})")
        return tid

    # --- orders ---------------------------------------------------------------

    @staticmethod
    def _order_input(order: Order) -> dict:
        body: dict[str, Any] = {
            "exchangeId": order.exchange_id,
            "side": order.side,
            "action": order.action,
            "quantity": order.quantity,
        }
        if order.price is not None:
            is_marker = (order.action == "buy" and order.price == 1.0) or (order.action == "sell" and order.price == 0.0)
            body["price"] = order.price if is_marker else format_price(order.price)
        body["tournamentId"] = order.tournament_id
        if order.expiration_date is not None:
            body["expirationDate"] = _iso(order.expiration_date)
        return body

    @staticmethod
    def _placed(order: Order, data: dict) -> Order:
        order_id = data.get("orderId")
        if order_id is None:
            # TODO(api): batch item `data` is untyped in the spec; assumed to
            # match the single-order response. No id -> leave PENDING so
            # reconciliation resolves it.
            return order
        return order.model_copy(update={
            "id": str(order_id),
            "status": OrderStatus.OPEN if data.get("open") else OrderStatus.FILLED,
            "terminal_reason_code": data.get("terminalReasonCode"),
            "created_at": datetime.now(timezone.utc),
        })  # fmt: skip

    async def place_order(self, order: Order) -> Order:
        await self._check(order.tournament_id)
        body = {**self._order_input(order), "idempotencyKey": order.idempotency_key}
        _, data = await self._send("POST", "/orders", label="POST /orders", body=body,
                                   unknown_key=order.idempotency_key, rejection=SigOrderRejected)  # fmt: skip
        return self._placed(order, data)

    async def place_batch(self, orders: list[Order], batch_key: str) -> list[BatchItemResult]:
        """Up to 50 independent orders under one idempotency key. Not atomic:
        a 207 mixes successes and failures. Transient items (429/5xx) are
        resumed with the same key; terminal failures are returned for the
        caller to resubmit, if at all, under a NEW batch key."""
        if not orders:
            return []
        if len(orders) > MAX_BATCH:
            raise ValueError(f"batch of {len(orders)} exceeds the {MAX_BATCH}-order limit")
        for o in orders:
            await self._check(o.tournament_id)
        body = {"idempotencyKey": batch_key, "orders": [self._order_input(o) for o in orders]}

        def classify(resp: httpx.Response, data: Any) -> str | None:
            results = data.get("results") if isinstance(data, dict) else None
            if resp.status_code in (200, 207, 422) and results is not None:
                statuses = [r.get("status", 0) for r in results]
                if any(s == 429 for s in statuses):
                    return "retry_rate_limited"
                if any(s >= 500 for s in statuses):
                    return "retry"
                return "done"
            if resp.status_code == 502 and results is not None:
                return "unknown"
            return None

        _, data = await self._send("POST", "/orders/batch", label="POST /orders/batch", body=body,
                                   classify=classify, unknown_key=batch_key, rejection=SigOrderRejected)  # fmt: skip
        out = []
        for r in data.get("results", []):
            o = orders[r["index"]]
            item = r.get("data") or {}
            if r.get("ok"):
                out.append(BatchItemResult(r["index"], True, r.get("status", 200), self._placed(o, item)))
            else:
                code = item.get("code") or (item.get("error") or {}).get("code", "")
                message = item.get("message") or (item.get("error") or {}).get("message", "")
                out.append(BatchItemResult(r["index"], False, r.get("status", 0), o, code, message))
        return out

    async def cancel(self, order_id: str, tournament_id: str) -> None:
        await self._check(tournament_id)

        def classify(resp: httpx.Response, data: Any) -> str | None:
            # 409: already filled or cancelled by a concurrent request - gone either way.
            if resp.status_code == 409 and _error_of(data)[0] != "REQUEST_IN_FLIGHT":
                return "done"
            return None

        await self._send("DELETE", f"/orders/{order_id}", label="DELETE /orders/{id}", classify=classify)

    async def cancel_all(
        self,
        tournament_id: str,
        exchange_id: str | None = None,
        market_id: str | None = None,
    ) -> CancelAllResult:
        """Scoped cancel-all, always with tournamentId. 503 (engine paused) is
        retried identically, which resumes it. 207/422 report `remaining` > 0:
        the caller must confirm via get_open_orders before re-posting."""
        if exchange_id is not None and market_id is not None:
            raise ValueError("exchange_id and market_id are mutually exclusive")
        tid = await self._check(tournament_id)
        body: dict[str, str] = {"tournamentId": tid}
        if exchange_id is not None:
            body["exchangeId"] = exchange_id
        if market_id is not None:
            body["marketId"] = market_id

        def classify(resp: httpx.Response, data: Any) -> str | None:
            return "done" if resp.status_code in (200, 207, 422) else None

        _, data = await self._send("POST", "/orders/cancel-all", label="POST /orders/cancel-all",
                                   body=body, classify=classify)  # fmt: skip
        return CancelAllResult(cancelled=int(data.get("cancelled", 0)), remaining=len(data.get("errors") or []))

    # --- reads ------------------------------------------------------------------

    async def get_markets(self, tournament_id: str) -> list[Market]:
        tid = await self._check(tournament_id)
        out: list[Market] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"tournamentId": tid, "limit": PAGE_LIMIT_MARKETS}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/markets", params)
            for m in data["data"]:
                out.append(Market(id=m["id"], title=m["title"], status=m["status"],
                                  settlement_date=m.get("settlementDate"), settled_with=m.get("settledWith"),
                                  settled_on=m.get("settledOn")))  # fmt: skip
            if not data["pagination"]["hasMore"]:
                return out
            cursor = data["pagination"]["nextCursor"]

    async def get_book(self, exchange_id: str, tournament_id: str, depth: int = 20) -> OrderBook:
        tid = await self._check(tournament_id)
        data = await self._get(f"/exchanges/{exchange_id}/orderbook", {"tournamentId": tid, "depth": depth})
        return OrderBook(
            exchange_id=exchange_id,
            tournament_id=tid,
            bids=[OrderBookLevel(price=lv["price"], quantity=lv["quantity"]) for lv in data.get("bids") or []],
            asks=[OrderBookLevel(price=lv["price"], quantity=lv["quantity"]) for lv in data.get("asks") or []],
        )

    async def get_top_of_books(
        self, exchange_ids: list[str], tournament_id: str
    ) -> dict[str, tuple[float | None, float | None]]:
        """Best (bid, ask) for many exchanges: GET /exchanges/prices, up to
        100 ids per request, Cup tournament scope."""
        tid = await self._check(tournament_id)
        out: dict[str, tuple[float | None, float | None]] = {}
        for i in range(0, len(exchange_ids), 100):
            chunk = exchange_ids[i : i + 100]
            data = await self._get("/exchanges/prices", {"ids": ",".join(chunk), "tournamentId": tid})
            for p in data.get("data") or []:
                out[str(p["exchangeId"])] = (p.get("bestBid"), p.get("bestAsk"))
        return out

    async def get_open_orders(self, tournament_id: str, exchange_id: str | None = None) -> list[Order]:
        tid = await self._check(tournament_id)
        out: list[Order] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"status": "open", "tournamentId": tid, "limit": PAGE_LIMIT_ORDERS}
            if exchange_id is not None:
                params["exchangeId"] = exchange_id
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/orders", params)
            for o in data["data"]:
                # TODO(api): the spec doesn't say whether priceLimit is
                # YES-normalized or side-relative for NO orders; kept raw.
                out.append(Order(
                    id=str(o["id"]), exchange_id=o["exchangeId"], tournament_id=tid,
                    side=o["side"], action=o["action"], quantity=int(o["quantity"]),
                    price=o.get("priceLimit"), expiration_date=o.get("expirationDate"),
                    # GET /orders doesn't return our key; mark it as venue-sourced.
                    idempotency_key=f"venue-order-{o['id']}",
                    status=OrderStatus.OPEN, created_at=o.get("createdAt"),
                ))  # fmt: skip
            if not data["pagination"]["hasMore"]:
                return out
            cursor = data["pagination"]["nextCursor"]

    async def get_positions(self, tournament_id: str) -> list[Position]:
        tid = await self._check(tournament_id)
        data = await self._get(f"/tournaments/{self._slug}/portfolio/positions")
        return [
            Position(exchange_id=p["exchangeId"], market_id=p["marketId"], tournament_id=tid,
                     quantity=p["quantity"], avg_cost=p["avgCost"], current_price=p.get("currentPrice"))  # fmt: skip
            for p in data.get("positions") or []
        ]

    async def get_new_fills(self, tournament_id: str, known_ids: set[str]) -> list[Fill]:
        """Fills not yet in `known_ids`, oldest first, from
        /tournaments/{slug}/portfolio/fills (newest first, paginated; stops at
        the first page holding a known fill). Includes manual trades on the
        account, which is what local positions must reflect.

        TODO(api): the spec's Fill has no `action`; `quantity` is
        "outcome-signed; negative values indicate the NO side". Read here as
        the signed change in the YES position (+ towards YES, - towards NO)
        and stored as a buy of that side. If a conventional closing sell is
        reported differently, reconciliation will show a mismatch and halt
        (fail closed); confirm against the first real fills."""
        tid = await self._check(tournament_id)
        new: list[Fill] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = await self._get(f"/tournaments/{self._slug}/portfolio/fills", params)
            page = data.get("data") or []
            hit_known = False
            for f in page:
                fid = str(f["id"])
                if fid in known_ids:
                    hit_known = True
                    continue
                qty = float(f["quantity"])
                if qty != int(qty):
                    raise ValueError(f"fill {fid} has fractional quantity {qty}")
                if qty == 0:
                    continue
                new.append(Fill(
                    id=fid, order_id=str(f.get("orderId") or ""), exchange_id=str(f["exchangeId"]),
                    tournament_id=tid, side="yes" if qty > 0 else "no", action="buy",
                    quantity=int(abs(qty)), price=float(f["price"]) if f.get("price") is not None else 0.0,
                    filled_at=f["filledAt"],
                ))  # fmt: skip
            if hit_known or not data["pagination"]["hasMore"]:
                return list(reversed(new))
            cursor = data["pagination"]["nextCursor"]

    async def get_pnl(self, tournament_id: str, period: str) -> TournamentPnl:
        """Cup P&L for `period` (day/week/month/quarter/year/all). Always the
        tournament path, never the no-argument /portfolio/pnl. A 409 (holdings
        without valuation prices) raises, never reads as 0."""
        if period not in PNL_PERIODS:
            raise ValueError(f"period must be one of {PNL_PERIODS}, got {period!r}")
        await self._check(tournament_id)
        d = await self._get(f"/tournaments/{self._slug}/portfolio/pnl", {"period": period})
        return TournamentPnl(
            period=d["period"],
            period_pnl=None if d.get("periodPnl") is None else float(d["periodPnl"]),
            unrealized_pnl=float(d["unrealizedPnl"]),
            total_account_value=float(d["totalAccountValue"]),
            roi=d.get("roi"),
        )

    async def get_balance(self, tournament_id: str) -> float:
        await self._check(tournament_id)
        data = await self._get(f"/tournaments/{self._slug}")
        return float(data["myBalance"])
