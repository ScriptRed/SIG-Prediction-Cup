"""Quoter v1: simplified Avellaneda-Stoikov around fair value (CLAUDE.md
"Key logic"), built for 20-30 markets at small size.

    reservation = fair_value - k * (position / max_position_shares)
    half_spread = max(min_edge, c * uncertainty)
    bid = floor_tick(reservation - half_spread), ask = ceil_tick(reservation + half_spread)

A side is dropped (never forced) when it falls outside [0.005, 0.995],
when the position limit is reached on that side, or when it would sit
further from fair value than the risk band
(risk.max_price_deviation_from_fair_value), so risk.check() never has to
reject a quote for that. With
post_only the quote backs off one tick behind the opposite SIG best price
rather than crossing it.

Each cycle re-quotes only the markets that need it: fair value moved >=
requote_move, position changed, quotes due for refresh before their short
expiry, or a market gained or lost its fair value (then it is pulled).
The OrderRouter cancels each of those markets on its own and posts the
new quotes in one batch. Blackout windows (quoter.pull_quotes_before_events)
pull everything and post nothing.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from predcup.control import TradingControl
from predcup.fairvalue import FairValue
from predcup.models import MAX_PRICE, MIN_PRICE, TICK, Order
from predcup.risk import within_price_band
from predcup.venues.sig import new_idempotency_key

_EPS = 1e-9


@dataclass(frozen=True)
class QuoterConfig:
    min_edge: float
    uncertainty_coefficient: float  # c
    inventory_skew_coefficient: float  # k, per full max_position_shares
    requote_move: float  # fair-value move that forces a re-quote
    quote_size: int  # shares per side
    expiration_seconds: int  # expirationDate on every quote (dead-man's switch)
    refresh_interval_seconds: int  # re-post before quotes expire
    max_markets: int
    max_position_shares: int
    max_price_deviation: float  # from risk; wider half-spreads mean no quote
    post_only: bool
    blackouts: tuple[tuple[datetime, datetime], ...]


def load_quoter_config(settings: dict) -> QuoterConfig:
    q = settings["quoter"]
    cfg = QuoterConfig(
        min_edge=float(q["min_edge"]),
        uncertainty_coefficient=float(q["uncertainty_coefficient"]),
        inventory_skew_coefficient=float(q["inventory_skew_coefficient"]),
        requote_move=int(q["requote_fair_value_move_ticks"]) * TICK,
        quote_size=int(q["quote_size_shares"]),
        expiration_seconds=int(q["quote_expiration_seconds"]),
        refresh_interval_seconds=int(q["refresh_interval_seconds"]),
        max_markets=int(q["max_markets"]),
        max_position_shares=int(q["max_position_shares"]),
        max_price_deviation=float(settings["risk"]["max_price_deviation_from_fair_value"]),
        post_only=bool(q["post_only"]),
        blackouts=tuple(
            (datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"]))
            for w in q.get("pull_quotes_before_events") or []
        ),
    )
    if cfg.refresh_interval_seconds >= cfg.expiration_seconds:
        raise ValueError("quoter.refresh_interval_seconds must be shorter than quote_expiration_seconds")
    return cfg


def _floor_tick(x: float) -> float:
    return round(math.floor(x / TICK + _EPS) * TICK, 3)


def _ceil_tick(x: float) -> float:
    return round(math.ceil(x / TICK - _EPS) * TICK, 3)


def in_blackout(now: datetime, windows: tuple[tuple[datetime, datetime], ...]) -> bool:
    return any(start <= now < end for start, end in windows)


@dataclass(frozen=True)
class TwoSidedQuote:
    bid: float | None
    ask: float | None
    reason: str = ""


def compute_quote(
    fv: FairValue,
    position: float,
    best_bid: float | None,
    best_ask: float | None,
    cfg: QuoterConfig,
) -> TwoSidedQuote:
    if not fv.ok or fv.value is None or fv.uncertainty is None:
        return TwoSidedQuote(None, None, f"no fair value: {fv.reason}")
    half = max(cfg.min_edge, cfg.uncertainty_coefficient * fv.uncertainty)
    if half > cfg.max_price_deviation + _EPS:
        return TwoSidedQuote(None, None, f"uncertainty too high: half-spread {half:.3f} > risk band {cfg.max_price_deviation}")

    inventory = max(-1.0, min(1.0, position / cfg.max_position_shares))
    reservation = fv.value - cfg.inventory_skew_coefficient * inventory
    bid: float | None = _floor_tick(reservation - half)
    ask: float | None = _ceil_tick(reservation + half)

    if cfg.post_only:
        if best_ask is not None and bid is not None and bid >= best_ask - _EPS:
            bid = _floor_tick(best_ask - TICK)
        if best_bid is not None and ask is not None and ask <= best_bid + _EPS:
            ask = _ceil_tick(best_bid + TICK)

    if bid is not None and (bid < MIN_PRICE - _EPS or position >= cfg.max_position_shares):
        bid = None
    if ask is not None and (ask > MAX_PRICE + _EPS or position <= -cfg.max_position_shares):
        ask = None
    # Never send a side risk.check() would reject for distance from fair
    # value (inventory skew or post-only back-off can push it out): the
    # exact function RiskManager uses.
    if bid is not None and not within_price_band(bid, fv.value, cfg.max_price_deviation):
        bid = None
    if ask is not None and not within_price_band(ask, fv.value, cfg.max_price_deviation):
        ask = None
    return TwoSidedQuote(bid, ask)


@dataclass(frozen=True)
class QuoteTarget:
    exchange_id: str
    market_id: str
    race_key: str
    party: str


@dataclass(frozen=True)
class CycleReport:
    requoted: bool
    markets: int = 0
    orders: int = 0
    reason: str = ""


BookReader = Callable[[list[str]], Awaitable[dict[str, tuple[float | None, float | None]]]]


@dataclass(frozen=True)
class _Posted:
    fair_value: float
    at: datetime
    position: float


class Quoter:
    def __init__(
        self,
        *,
        router,  # OrderRouter
        fair_values,  # FairValueTracker
        positions: Callable[[], dict[str, float]],  # exchange_id -> signed shares
        books: BookReader,  # SIG best (bid, ask) per exchange
        cfg: QuoterConfig,
        tournament_id: str,
        control: TradingControl,
    ) -> None:
        self._router = router
        self._fair_values = fair_values
        self._positions = positions
        self._books = books
        self._cfg = cfg
        self._tid = tournament_id
        self._control = control
        self._posted: dict[str, _Posted] = {}  # markets with our quotes resting

    def _due(self, ex: str, fv: float, position: float, now: datetime) -> str:
        posted = self._posted.get(ex)
        if posted is None:
            return "new"
        if (now - posted.at).total_seconds() >= self._cfg.refresh_interval_seconds:
            return "refresh before expiry"
        if abs(fv - posted.fair_value) >= self._cfg.requote_move - _EPS:
            return "fair value moved"
        if position != posted.position:
            return "position changed"
        return ""

    def _orders(self, t: QuoteTarget, q: TwoSidedQuote, now: datetime) -> list[Order]:
        expiry = now + timedelta(seconds=self._cfg.expiration_seconds)
        common = dict(exchange_id=t.exchange_id, market_id=t.market_id, tournament_id=self._tid,
                      party_id=t.party, race_key=t.race_key, side="yes", quantity=self._cfg.quote_size,
                      expiration_date=expiry)  # fmt: skip
        out = []
        if q.bid is not None:
            out.append(Order(**common, action="buy", price=q.bid, idempotency_key=new_idempotency_key()))
        if q.ask is not None:
            out.append(Order(**common, action="sell", price=q.ask, idempotency_key=new_idempotency_key()))
        return out

    async def _send(self, updates: dict[str, list[tuple[Order, FairValue]]], state: dict[str, _Posted], now: datetime) -> CycleReport:
        if not updates:
            return CycleReport(False, markets=len(self._posted), reason="nothing due")
        result = await self._router.requote(updates, now)
        for ex in result.done:
            if updates[ex]:
                self._posted[ex] = state[ex]
            else:
                self._posted.pop(ex, None)
        return CycleReport(True, markets=len(updates), orders=sum(len(v) for v in updates.values()),
                           reason=result.blocked or "requoted")  # fmt: skip

    async def cycle(self, targets: list[QuoteTarget], now: datetime) -> CycleReport:
        if self._control.halted:
            return CycleReport(False, reason=f"halted: {self._control.reason}")
        if in_blackout(now, self._cfg.blackouts):
            report = await self._send({ex: [] for ex in self._posted}, {}, now)
            return CycleReport(report.requoted, reason="blackout window")

        targets = targets[: self._cfg.max_markets]
        live = [(t, self._fair_values.current(t.exchange_id)) for t in targets]
        live = [(t, fv) for t, fv in live if fv.ok]
        books = await self._books([t.exchange_id for t, _ in live]) if live else {}
        positions = self._positions()

        updates: dict[str, list[tuple[Order, FairValue]]] = {}
        state: dict[str, _Posted] = {}
        quoted: set[str] = set()
        for t, fv in live:
            position = positions.get(t.exchange_id, 0)
            best_bid, best_ask = books.get(t.exchange_id, (None, None))
            q = compute_quote(fv, position, best_bid, best_ask, self._cfg)
            if q.bid is None and q.ask is None:
                continue
            quoted.add(t.exchange_id)
            if self._due(t.exchange_id, fv.value, position, now):  # type: ignore[arg-type]
                updates[t.exchange_id] = [(o, fv) for o in self._orders(t, q, now)]
                state[t.exchange_id] = _Posted(fv.value, now, position)  # type: ignore[arg-type]
        for ex in self._posted:
            if ex not in quoted:
                updates[ex] = []  # lost its fair value or quote: pull it
        return await self._send(updates, state, now)
