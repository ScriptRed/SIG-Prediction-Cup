"""Pydantic v2 models shared across venues, risk and strategies.

`tournament_id` has no default anywhere in this module. The platform has no
usable default tournament for our organization-bound key (see
docs/platform/SUMMARY.md) — a caller must always resolve and pass the Cup's
tournament id explicitly.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

TICK = 0.005
MIN_PRICE = 0.005
MAX_PRICE = 0.995
MAX_QUANTITY = 2_147_483_647  # engine holds quantity as a signed 32-bit int

Side = Literal["yes", "no"]
Action = Literal["buy", "sell"]


def is_on_tick(price: float) -> bool:
    """True if price is a multiple of the 0.005 tick within [0.005, 0.995]."""
    if price < MIN_PRICE - 1e-9 or price > MAX_PRICE + 1e-9:
        return False
    ticks = round(price / TICK)
    return abs(price - ticks * TICK) < 1e-9


class OrderStatus(str, Enum):
    PENDING = "pending"  # submitted locally, not yet confirmed by the venue
    OPEN = "open"  # venue-confirmed resting order
    FILLED = "filled"
    CANCELLED = "cancelled"  # venue-confirmed cancelled
    EXPIRED = "expired"  # venue-confirmed expired
    REJECTED = "rejected"


class Order(BaseModel):
    id: str | None = None  # platform orderId, set once the venue accepts it
    exchange_id: str
    market_id: str | None = None
    tournament_id: str
    party_id: str | None = None  # real-world party/candidate this exposure backs
    side: Side
    action: Action
    quantity: int
    price: float | None = None  # None = market order
    expiration_date: datetime | None = None
    idempotency_key: str
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime | None = None
    terminal_reason_code: str | None = None

    @field_validator("tournament_id")
    @classmethod
    def tournament_id_not_blank(cls, v: str) -> str:
        if not v:
            raise ValueError("tournament_id is required and cannot be blank")
        return v

    @field_validator("quantity")
    @classmethod
    def quantity_in_range(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("quantity must be positive")
        if v > MAX_QUANTITY:
            raise ValueError(f"quantity exceeds engine max of {MAX_QUANTITY}")
        return v

    @model_validator(mode="after")
    def price_on_tick_or_market_marker(self) -> "Order":
        if self.price is None:
            return self
        is_buy_market_marker = self.action == "buy" and self.price == 1.0
        is_sell_market_marker = self.action == "sell" and self.price == 0.0
        if is_buy_market_marker or is_sell_market_marker:
            return self
        if not is_on_tick(self.price):
            raise ValueError(
                f"price {self.price} is off-tick; must be a 0.005 multiple "
                f"in [{MIN_PRICE}, {MAX_PRICE}], or the market-order marker "
                "(1 for buy, 0 for sell)"
            )
        return self

    @model_validator(mode="after")
    def expiration_only_on_limit_orders(self) -> "Order":
        if self.price is None and self.expiration_date is not None:
            raise ValueError("expirationDate is not supported for market orders")
        return self


class Fill(BaseModel):
    id: str
    order_id: str
    exchange_id: str
    tournament_id: str
    side: Side
    action: Action
    quantity: int
    price: float
    filled_at: datetime

    @field_validator("tournament_id")
    @classmethod
    def tournament_id_not_blank(cls, v: str) -> str:
        if not v:
            raise ValueError("tournament_id is required and cannot be blank")
        return v


class Position(BaseModel):
    exchange_id: str
    market_id: str
    tournament_id: str
    party_id: str | None = None
    # Positive = YES shares, negative = NO shares (sign encodes side).
    quantity: float
    avg_cost: float
    current_price: float | None = None

    @field_validator("tournament_id")
    @classmethod
    def tournament_id_not_blank(cls, v: str) -> str:
        if not v:
            raise ValueError("tournament_id is required and cannot be blank")
        return v


class Market(BaseModel):
    id: str
    title: str
    status: Literal["open", "closed", "settled"]
    settlement_date: datetime | None = None
    settled_with: str | None = None
    settled_on: datetime | None = None


class OrderBookLevel(BaseModel):
    price: float
    quantity: float


class OrderBook(BaseModel):
    exchange_id: str
    tournament_id: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]

    @field_validator("tournament_id")
    @classmethod
    def tournament_id_not_blank(cls, v: str) -> str:
        if not v:
            raise ValueError("tournament_id is required and cannot be blank")
        return v
