"""In-memory fake implementing predcup.venues.base.Venue, for offline tests.

Every strategy must run against this before going live (CLAUDE.md). It
models the SIG platform's tournament isolation (separate order books and
balances per tournament_id) but is deliberately simple: no price-time
matching engine, no FIFO lot tracking — orders rest until explicitly
cancelled or filled via `simulate_fill`. Fills are reported the way
SigVenue.get_new_fills reports them: side = the direction the YES position
moved (a YES sell shows as a NO-side fill), action "buy".
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

from predcup.models import Fill, Market, Order, OrderBook, OrderStatus, Position
from predcup.venues.base import CancelAllResult, Venue


class MockExchange(Venue):
    def __init__(
        self,
        markets: list[Market] | None = None,
        books: dict[str, OrderBook] | None = None,
        starting_balance: float = 100_000.0,
    ) -> None:
        self._markets = markets or []
        self._books = books or {}
        self._starting_balance = starting_balance
        self._order_id_counter = itertools.count(1)
        # tournament_id -> order_id -> Order
        self._open_orders: dict[str, dict[str, Order]] = {}
        # tournament_id -> exchange_id -> Position
        self._positions: dict[str, dict[str, Position]] = {}
        self._balances: dict[str, float] = {}
        # order_id -> remaining cancel_all calls that should report it
        # cancelled while actually leaving it open (simulating a venue-side
        # silent miss). float("inf") means miss on every call indefinitely.
        self._cancel_all_miss_counts: dict[str, float] = {}
        self._fills: dict[str, list[Fill]] = {}  # tournament_id -> fills, oldest first
        self._fill_ids = itertools.count(1)
        self._tops: dict[str, tuple[float | None, float | None]] = {}

    def configure_cancel_all_to_silently_miss(
        self, order_ids: set[str], times: float = float("inf")
    ) -> None:
        for order_id in order_ids:
            self._cancel_all_miss_counts[order_id] = times

    def _orders_for(self, tournament_id: str) -> dict[str, Order]:
        return self._open_orders.setdefault(tournament_id, {})

    async def get_markets(self, tournament_id: str) -> list[Market]:
        return list(self._markets)

    async def get_book(self, exchange_id: str, tournament_id: str) -> OrderBook:
        if exchange_id in self._books:
            return self._books[exchange_id]
        return OrderBook(exchange_id=exchange_id, tournament_id=tournament_id, bids=[], asks=[])

    async def place_order(self, order: Order) -> Order:
        order_id = str(next(self._order_id_counter))
        placed = order.model_copy(
            update={
                "id": order_id,
                "status": OrderStatus.OPEN,
                "created_at": datetime.now(timezone.utc),
            }
        )
        self._orders_for(order.tournament_id)[order_id] = placed
        self._balances.setdefault(order.tournament_id, self._starting_balance)
        return placed

    async def cancel(self, order_id: str, tournament_id: str) -> None:
        self._orders_for(tournament_id).pop(order_id, None)

    async def cancel_all(
        self,
        tournament_id: str,
        exchange_id: str | None = None,
        market_id: str | None = None,
    ) -> CancelAllResult:
        orders = self._orders_for(tournament_id)
        in_scope_ids = [
            oid
            for oid, o in orders.items()
            if (exchange_id is None or o.exchange_id == exchange_id)
            and (market_id is None or o.market_id == market_id)
        ]
        cancelled = 0
        for oid in in_scope_ids:
            remaining_misses = self._cancel_all_miss_counts.get(oid, 0)
            if remaining_misses > 0:
                # Silently miss: report it as cancelled but leave it resting.
                if remaining_misses != float("inf"):
                    self._cancel_all_miss_counts[oid] = remaining_misses - 1
                cancelled += 1
                continue
            del orders[oid]
            cancelled += 1
        return CancelAllResult(cancelled=cancelled, remaining=0)

    async def get_open_orders(
        self, tournament_id: str, exchange_id: str | None = None
    ) -> list[Order]:
        orders = self._orders_for(tournament_id).values()
        if exchange_id is not None:
            orders = [o for o in orders if o.exchange_id == exchange_id]
        return list(orders)

    async def get_positions(self, tournament_id: str) -> list[Position]:
        return list(self._positions.get(tournament_id, {}).values())

    async def get_balance(self, tournament_id: str) -> float:
        return self._balances.get(tournament_id, self._starting_balance)

    # --- simulation helpers (tests, multi-hour mock run) -----------------------

    def set_top_of_book(self, exchange_id: str, bid: float | None, ask: float | None) -> None:
        self._tops[exchange_id] = (bid, ask)

    async def get_top_of_books(
        self, exchange_ids: list[str], tournament_id: str
    ) -> dict[str, tuple[float | None, float | None]]:
        return {e: self._tops.get(e, (None, None)) for e in exchange_ids}

    async def simulate_fill(self, order_id: str, quantity: int, tournament_id: str | None = None) -> Fill:
        """Fill `quantity` of a resting order at its limit price."""
        tids = [tournament_id] if tournament_id else list(self._open_orders)
        for tid in tids:
            orders = self._orders_for(tid)
            if order_id in orders:
                break
        else:
            raise KeyError(f"no open order {order_id}")
        order = orders[order_id]
        qty = min(quantity, order.quantity)
        towards_yes = (order.side == "yes") == (order.action == "buy")
        yes_price = order.price if order.side == "yes" else 1 - (order.price or 0)
        fill = Fill(id=str(next(self._fill_ids)), order_id=order_id, exchange_id=order.exchange_id, tournament_id=tid,
                    side="yes" if towards_yes else "no", action="buy", quantity=qty, price=yes_price or 0.0,
                    filled_at=datetime.now(timezone.utc))  # fmt: skip
        self._fills.setdefault(tid, []).append(fill)
        positions = self._positions.setdefault(tid, {})
        signed = qty if towards_yes else -qty
        prev = positions.get(order.exchange_id)
        new_qty = (prev.quantity if prev else 0) + signed
        positions[order.exchange_id] = Position(exchange_id=order.exchange_id, market_id=order.market_id or "",
                                                tournament_id=tid, quantity=new_qty, avg_cost=yes_price or 0.0,
                                                current_price=yes_price)  # fmt: skip
        if qty >= order.quantity:
            del orders[order_id]
        else:
            orders[order_id] = order.model_copy(update={"quantity": order.quantity - qty})
        return fill

    async def get_new_fills(self, tournament_id: str, known_ids: set[str]) -> list[Fill]:
        return [f for f in self._fills.get(tournament_id, []) if f.id not in known_ids]

    async def get_pnl(self, tournament_id: str, period: str):
        """The mock doesn't value positions: report a flat day (0 P&L) so
        live-mode runs have a daily P&L, as the real adapter provides."""
        from predcup.venues.sig import TournamentPnl

        balance = self._balances.get(tournament_id, self._starting_balance)
        return TournamentPnl(period=period, period_pnl=0.0, unrealized_pnl=0.0, total_account_value=balance, roi=None)
