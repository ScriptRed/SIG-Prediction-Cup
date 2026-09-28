"""Every order goes through RiskManager.check() (CLAUDE.md Hard Rule 1). No
code path may call a venue's place_order directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from predcup.models import Order, OrderStatus
from predcup.store import EventStore
from predcup.venues.base import Venue


class Alerter(Protocol):
    def send(self, message: str) -> None: ...


@dataclass(frozen=True)
class RiskLimits:
    max_bankroll_fraction_per_market: float
    max_total_exposure_fraction: float
    # Net exposure to one party/candidate across all races. Per-market caps
    # alone don't cover this: several different race markets can all be a
    # bet on the same party, and a polling-error scenario moves them
    # together, so the aggregate needs its own budget.
    max_party_exposure_fraction: float
    max_order_size_susqies: float
    max_price_deviation_from_fair_value: float
    daily_loss_stop_fraction: float
    stale_data_stop_seconds: float


def _require(section: dict, key: str) -> float:
    if key not in section:
        raise KeyError(f"config['risk'] is missing required key '{key}'")
    return section[key]


def load_risk_limits(config: dict) -> RiskLimits:
    section = config["risk"]
    return RiskLimits(
        max_bankroll_fraction_per_market=_require(section, "max_bankroll_fraction_per_market"),
        max_total_exposure_fraction=_require(section, "max_total_exposure_fraction"),
        max_party_exposure_fraction=_require(section, "max_party_exposure_fraction"),
        max_order_size_susqies=_require(section, "max_order_size_susqies"),
        max_price_deviation_from_fair_value=_require(
            section, "max_price_deviation_from_fair_value"
        ),
        daily_loss_stop_fraction=_require(section, "daily_loss_stop_fraction"),
        stale_data_stop_seconds=_require(section, "stale_data_stop_seconds"),
    )


@dataclass
class RiskDecision:
    approved: bool
    reason: str | None = None


def is_exposure_counted(status: OrderStatus) -> bool:
    """An order's exposure counts unless the venue has *confirmed* it
    cancelled or expired. `OrderStatus.OPEN` counts even past a local
    `expiration_date` — the platform's `open` flag can stay true after
    expiry and expiry emits no realtime event (docs/platform/SUMMARY.md), so
    only an explicit RiskManager.confirm_order_state() call (driven by a
    reconciliation read) may remove an order from exposure.
    # TODO(api): verify live — confirm `status=expired` polling and the
    # `open` flag behave exactly as SUMMARY.md describes before relying on
    # this in production; the spec's wording was inferred from prose, not
    # exercised against the real trading engine yet.
    """
    return status not in (OrderStatus.CANCELLED, OrderStatus.EXPIRED)


def _order_notional(order: Order) -> float:
    price = order.price if order.price is not None else 1.0
    return order.quantity * price


def _total_notional(orders: list[Order]) -> float:
    return sum(_order_notional(o) for o in orders if is_exposure_counted(o.status))


def _market_notional(orders: list[Order], market_id: str | None) -> float:
    return _total_notional([o for o in orders if o.market_id == market_id])


def _party_notional(orders: list[Order], party_id: str | None) -> float:
    return _total_notional([o for o in orders if o.party_id == party_id])


class RiskManager:
    def __init__(
        self,
        limits: RiskLimits,
        bankroll: float,
        event_store: EventStore,
        venue: Venue,
        tournament_id: str,
        alerter: Alerter,
    ) -> None:
        if not tournament_id:
            raise ValueError("tournament_id is required and cannot be blank")
        self._limits = limits
        self._bankroll = bankroll
        self._event_store = event_store
        self._venue = venue
        self._tournament_id = tournament_id
        self._alerter = alerter
        self._daily_realized_pnl = 0.0
        self._orders: dict[str, Order] = {}

    def update_bankroll(self, bankroll: float) -> None:
        self._bankroll = bankroll

    def update_daily_pnl(self, pnl: float) -> None:
        self._daily_realized_pnl = pnl

    def record_order(self, order: Order) -> None:
        """Track a placed order so its exposure counts until confirmed
        cancelled or expired (see is_exposure_counted)."""
        key = order.id or order.idempotency_key
        self._orders[key] = order

    def confirm_order_state(self, order_key: str, status: OrderStatus) -> None:
        if order_key in self._orders:
            self._orders[order_key] = self._orders[order_key].model_copy(update={"status": status})

    def _tracked_orders(self) -> list[Order]:
        return list(self._orders.values())

    def check(self, order: Order, *, fair_value: float | None, outside_data_age_seconds: float) -> RiskDecision:
        decision = self._evaluate(order, fair_value=fair_value, outside_data_age_seconds=outside_data_age_seconds)
        if not decision.approved:
            self._event_store.log(
                "risk_rejection",
                {
                    "market_id": order.market_id,
                    "exchange_id": order.exchange_id,
                    "party_id": order.party_id,
                    "quantity": order.quantity,
                    "price": order.price,
                    "reason": decision.reason,
                },
            )
        return decision

    def _evaluate(
        self, order: Order, *, fair_value: float | None, outside_data_age_seconds: float
    ) -> RiskDecision:
        limits = self._limits

        if outside_data_age_seconds > limits.stale_data_stop_seconds:
            return RiskDecision(False, "stale outside data")

        if self._bankroll > 0 and (self._daily_realized_pnl / self._bankroll) <= -limits.daily_loss_stop_fraction:
            return RiskDecision(False, "daily loss stop triggered")

        order_notional = _order_notional(order)

        if order_notional > limits.max_order_size_susqies:
            return RiskDecision(False, "exceeds max order size")

        if order.price is not None and fair_value is not None:
            if abs(order.price - fair_value) > limits.max_price_deviation_from_fair_value:
                return RiskDecision(False, "price deviates too far from fair value")

        tracked = self._tracked_orders()

        market_cap = limits.max_bankroll_fraction_per_market * self._bankroll
        if _market_notional(tracked, order.market_id) + order_notional > market_cap:
            return RiskDecision(False, "exceeds per-market bankroll cap")

        if order.party_id is not None:
            party_cap = limits.max_party_exposure_fraction * self._bankroll
            if _party_notional(tracked, order.party_id) + order_notional > party_cap:
                return RiskDecision(False, "exceeds net party-exposure cap")

        total_cap = limits.max_total_exposure_fraction * self._bankroll
        if _total_notional(tracked) + order_notional > total_cap:
            return RiskDecision(False, "exceeds total exposure cap")

        return RiskDecision(True)
