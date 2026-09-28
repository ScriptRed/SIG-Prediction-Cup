"""Abstract Venue interface. `sim/mock_exchange.py` and every real adapter
(`sig.py`, `kalshi.py`, `polymarket.py`) implement this.

`tournament_id` is a required positional parameter with no default on every
method here. The platform has no usable default tournament for our
org-bound key (an omitted tournamentId silently falls back to the
organization's default tournament, which may not be the Predictions Cup —
see docs/platform/SUMMARY.md). Never give these parameters a default.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel

from predcup.models import Market, Order, OrderBook, Position


class CancelAllResult(BaseModel):
    cancelled: int
    remaining: int

    @property
    def all_cancelled(self) -> bool:
        return self.remaining == 0


class Venue(ABC):
    @abstractmethod
    async def get_markets(self, tournament_id: str) -> list[Market]: ...

    @abstractmethod
    async def get_book(self, exchange_id: str, tournament_id: str) -> OrderBook: ...

    @abstractmethod
    async def place_order(self, order: Order) -> Order: ...

    @abstractmethod
    async def cancel(self, order_id: str, tournament_id: str) -> None: ...

    @abstractmethod
    async def cancel_all(
        self,
        tournament_id: str,
        exchange_id: str | None = None,
        market_id: str | None = None,
    ) -> CancelAllResult: ...

    @abstractmethod
    async def get_open_orders(
        self, tournament_id: str, exchange_id: str | None = None
    ) -> list[Order]: ...

    @abstractmethod
    async def get_positions(self, tournament_id: str) -> list[Position]: ...

    @abstractmethod
    async def get_balance(self, tournament_id: str) -> float: ...
