"""Every order goes through RiskManager.check() (CLAUDE.md Hard Rule 1). No
code path may call a venue's place_order directly.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from predcup.models import Fill, Order, OrderStatus
from predcup.store import EventStore
from predcup.venues.base import Venue

MARKOUT_HORIZONS_MINUTES = (1, 5, 30)


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


@dataclass(frozen=True)
class SizeRampConfig:
    """Launch sizing: order-size and per-market limits start at
    launch_fraction of their configured values and multiply by
    step_multiplier after every clean_reconciliations_per_step clean
    reconciliations in a row, capped at 1.0 (full configured size).
    More than rate_limit_max_in_window 429s within
    rate_limit_window_seconds count as one failure.
    launch_fraction = 1.0 disables the ramp.
    """

    launch_fraction: float
    step_multiplier: float
    clean_reconciliations_per_step: int
    rate_limit_max_in_window: int
    rate_limit_window_seconds: float

    def __post_init__(self) -> None:
        if not 0 < self.launch_fraction <= 1:
            raise ValueError("size_ramp.launch_fraction must be in (0, 1]")
        if self.step_multiplier <= 1:
            raise ValueError("size_ramp.step_multiplier must be > 1")
        if self.clean_reconciliations_per_step < 1:
            raise ValueError("size_ramp.clean_reconciliations_per_step must be >= 1")
        if self.rate_limit_max_in_window < 1:
            raise ValueError("size_ramp.rate_limit_max_in_window must be >= 1")
        if self.rate_limit_window_seconds <= 0:
            raise ValueError("size_ramp.rate_limit_window_seconds must be > 0")


def load_size_ramp_config(config: dict) -> SizeRampConfig:
    if "size_ramp" not in config["risk"]:
        raise KeyError("config['risk'] is missing required key 'size_ramp'")
    section = config["risk"]["size_ramp"]
    return SizeRampConfig(
        launch_fraction=_require(section, "launch_fraction"),
        step_multiplier=_require(section, "step_multiplier"),
        clean_reconciliations_per_step=int(_require(section, "clean_reconciliations_per_step")),
        rate_limit_max_in_window=int(_require(section, "rate_limit_max_in_window")),
        rate_limit_window_seconds=_require(section, "rate_limit_window_seconds"),
    )


_FULL_SIZE_TOLERANCE = 1e-9


def ramp_multiplier(config: SizeRampConfig, step: int) -> float:
    m = config.launch_fraction * config.step_multiplier**step
    return 1.0 if m >= 1.0 - _FULL_SIZE_TOLERANCE else m


def max_ramp_step(config: SizeRampConfig) -> int:
    step = 0
    while ramp_multiplier(config, step) < 1.0:
        step += 1
    return step


RAMP_FAILURE_KINDS = ("reconciliation_mismatch", "unexpected_4xx", "rate_limit_burst")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SizeRamp:
    """Step state for the launch size ramp, persisted in SQLite on every
    change. On construction it resumes one step below the saved step
    (floor 0 = launch_fraction): we don't know why the process died, so we
    don't trust the old level fully. That resumed step is saved at once,
    so a crash loop walks the ramp down rather than holding it up.
    """

    def __init__(
        self,
        config: SizeRampConfig,
        event_store: EventStore,
        alerter: Alerter,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._config = config
        self._event_store = event_store
        self._alerter = alerter
        self._now = now
        self._max_step = max_ramp_step(config)
        self._rate_limit_times: deque[datetime] = deque()
        saved = event_store.load_ramp_step()
        self.step = 0 if saved is None else min(max(0, saved - 1), self._max_step)
        self.clean_count = 0
        event_store.save_ramp_step(self.step)
        self._log("init", saved_step=saved)

    @property
    def multiplier(self) -> float:
        return ramp_multiplier(self._config, self.step)

    @property
    def at_full_size(self) -> bool:
        return self.step >= self._max_step

    def record_clean_reconciliation(self) -> None:
        if self.at_full_size:
            return
        self.clean_count += 1
        self._log("clean_reconciliation")
        if self.clean_count >= self._config.clean_reconciliations_per_step:
            self.step += 1
            self.clean_count = 0
            self._event_store.save_ramp_step(self.step)
            self._log("step_up")
            self._alerter.send(
                f"Size ramp stepped up to step {self.step}/{self._max_step} "
                f"({self.multiplier:.0%} of configured size)."
            )

    def record_failure(self, kind: str, detail: str) -> None:
        if kind not in RAMP_FAILURE_KINDS:
            raise ValueError(f"unknown size-ramp failure kind {kind!r}")
        previous = self.step
        self.step = max(0, self.step - 1)
        self.clean_count = 0
        self._event_store.save_ramp_step(self.step)
        self._log("step_down", failure_kind=kind, detail=detail, previous_step=previous)
        self._alerter.send(
            f"Size ramp: {kind} ({detail}). Step {previous} -> {self.step}/{self._max_step} "
            f"({self.multiplier:.0%} of configured size); clean count reset."
        )

    def record_rate_limited(self, detail: str) -> None:
        """Every 429 lands here, including ones the adapter then retried
        successfully. One 429 alerts but leaves step and clean count alone
        (the adapter has already backed off its request rate); more than
        rate_limit_max_in_window inside the window is a failure."""
        now = self._now()
        window_start = now - timedelta(seconds=self._config.rate_limit_window_seconds)
        self._rate_limit_times.append(now)
        while self._rate_limit_times and self._rate_limit_times[0] <= window_start:
            self._rate_limit_times.popleft()
        count = len(self._rate_limit_times)
        self._log("rate_limited", detail=detail, rate_limits_in_window=count)
        self._alerter.send(
            f"Rate limited: {detail}. {count} 429(s) in the last "
            f"{self._config.rate_limit_window_seconds:g}s "
            f"(ramp drops above {self._config.rate_limit_max_in_window})."
        )
        if count > self._config.rate_limit_max_in_window:
            self._rate_limit_times.clear()
            self.record_failure(
                "rate_limit_burst",
                f"{count} 429s within {self._config.rate_limit_window_seconds:g}s; last: {detail}",
            )

    def reset(self, reason: str) -> None:
        """Manual reset to launch_fraction (Telegram /resetramp)."""
        previous = self.step
        self.step = 0
        self.clean_count = 0
        self._event_store.save_ramp_step(0)
        self._log("reset", detail=reason, previous_step=previous)
        self._alerter.send(
            f"Size ramp reset to launch size ({self.multiplier:.0%}) from step {previous}: {reason}"
        )

    def _log(self, action: str, **extra: object) -> None:
        self._event_store.log(
            "size_ramp",
            {
                "action": action,
                "step": self.step,
                "max_step": self._max_step,
                "multiplier": self.multiplier,
                "clean_count": self.clean_count,
                "clean_reconciliations_per_step": self._config.clean_reconciliations_per_step,
                **extra,
            },
        )


@dataclass
class RiskDecision:
    approved: bool
    reason: str | None = None


@dataclass
class KillSwitchResult:
    success: bool
    attempts: int
    remaining_order_ids: list[str]


_EXPOSURE_FREEING_STATUSES = (OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED)


def is_exposure_counted(status: OrderStatus) -> bool:
    """An order's exposure counts unless the venue has *confirmed* it
    cancelled, expired, or rejected (never went live). `OrderStatus.OPEN`
    counts even past a local `expiration_date` — the platform's `open` flag
    can stay true after expiry and expiry emits no realtime event
    (docs/platform/SUMMARY.md), so only an explicit
    RiskManager.confirm_order_state() call (driven by a reconciliation
    read) may remove an order from exposure.
    # TODO(api): verify live — confirm `status=expired` polling and the
    # `open` flag behave exactly as SUMMARY.md describes before relying on
    # this in production; the spec's wording was inferred from prose, not
    # exercised against the real trading engine yet.
    """
    return status not in _EXPOSURE_FREEING_STATUSES


def _order_notional(order: Order) -> float:
    price = order.price if order.price is not None else 1.0
    return order.quantity * price


def _total_notional(orders: list[Order]) -> float:
    return sum(_order_notional(o) for o in orders if is_exposure_counted(o.status))


def _market_notional(orders: list[Order], market_id: str | None) -> float:
    return _total_notional([o for o in orders if o.market_id == market_id])


def _is_long(side: str, action: str) -> bool:
    """True if this side/action combination benefits from *this market's
    own* YES-normalized price rising — i.e. is "long" this market's own
    outcome (its own party, for an election market)."""
    return (side == "yes" and action == "buy") or (side == "no" and action == "sell")


# Independent-party markets are deliberately excluded from the R-vs-D axis
# below. Betting against the Republican in a two-party race is, to a good
# approximation, the same as betting on the Democrat (and vice versa) — the
# whole point of this limit is to catch that correlated "red wave" /
# "blue wave" exposure. But betting against the Republican in a race that
# *also* has a serious Independent doesn't reliably say anything about the
# Democrat specifically: the shifted probability mass could go to the
# independent instead. Folding Independent exposure into either major
# party's bucket would misrepresent that race's risk, so it gets its own
# lane instead — subject only to the per-market and total-exposure caps,
# not this one. Revisit if a session's exposure to independents grows
# large enough that idiosyncratic risk there stops being a rounding error.
_RD_PARTIES = ("R", "D")

# Fusion races (SIG rules, 2026-09-30): a fusion candidate counts for every
# party on the ticket, so R and D can *both* resolve YES. There R and D are
# not complements, so R-YES and D-NO (or R-YES and D-YES as a "hedge") must
# not combine or offset on this axis. Like Independents, a fusion race's
# orders get their own lane: per-market and total caps only. The set comes
# from market_map.csv's fusion_risk column (predcup.market_map).


def net_rd_exposure(orders: list[Order], fusion_race_keys: frozenset[str]) -> float:
    """Net directional exposure on the national R-vs-D axis: positive =
    net long Republican (short Democrat) across every race, negative = net
    long Democrat. Only orders with party_id in {"R", "D"} participate.

    The sum is race-agnostic — R-YES and D-NO in the *same* race both add
    the same direction (that's the motivating case), but so do R-YES in
    one race and D-NO in a completely different one, because this limit
    exists to cap national correlated exposure, not a single race's
    pairing. Grouping by race_key isn't needed for the arithmetic, only
    for callers reasoning about *why* two orders combine. Orders in
    `fusion_race_keys` are left out entirely (see above).
    """
    net = 0.0
    for o in orders:
        if not is_exposure_counted(o.status) or o.party_id not in _RD_PARTIES:
            continue
        if o.race_key in fusion_race_keys:
            continue
        signed = _order_notional(o) * (1 if _is_long(o.side, o.action) else -1)
        net += signed if o.party_id == "R" else -signed
    return net


@dataclass(frozen=True)
class PositionExposure:
    """A venue position as risk sees it (fed by the reconciliation loop).
    quantity: + YES shares / - NO shares; price: YES valuation price."""

    market_id: str
    party_id: str | None
    race_key: str | None
    quantity: float
    price: float

    @property
    def notional(self) -> float:
        return self.quantity * self.price if self.quantity >= 0 else -self.quantity * (1 - self.price)

    @property
    def is_long(self) -> bool:
        """Long this market's own outcome (its party, for an election market)."""
        return self.quantity > 0


def net_rd_positions(positions: list[PositionExposure], fusion_race_keys: frozenset[str]) -> float:
    """Positions' contribution to net_rd_exposure (same sign convention and
    the same fusion/Independent exclusions)."""
    net = 0.0
    for p in positions:
        if p.party_id not in _RD_PARTIES or p.race_key in fusion_race_keys:
            continue
        signed = p.notional * (1 if p.is_long else -1)
        net += signed if p.party_id == "R" else -signed
    return net


def compute_markout(fill: Fill, later_price: float) -> float:
    """Markout in probability points, positive = the fill looks good so
    far. Assumes fill.price is already YES-normalized, per CLAUDE.md's
    convention that all internal prices are ("Convert at the venue boundary
    only") — so later_price (also YES-normalized) is directly comparable.
    """
    if _is_long(fill.side, fill.action):
        return later_price - fill.price
    return fill.price - later_price


class RiskManager:
    def __init__(
        self,
        limits: RiskLimits,
        bankroll: float,
        event_store: EventStore,
        venue: Venue,
        tournament_id: str,
        alerter: Alerter,
        size_ramp: SizeRamp,
        fusion_race_keys: frozenset[str] | None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not tournament_id:
            raise ValueError("tournament_id is required and cannot be blank")
        # Fail closed: there is no "no ramp = full size" mode. Disabling
        # the ramp is an explicit config change (launch_fraction: 1.0).
        if size_ramp is None:
            raise ValueError("size_ramp is required; RiskManager never runs without one")
        # Fail closed: an explicit (possibly empty) set, never a default, so a
        # caller can't forget it and silently net fusion races as R-vs-D.
        if fusion_race_keys is None:
            raise ValueError("fusion_race_keys is required (pass frozenset() if no race has fusion risk)")
        self._fusion_race_keys = frozenset(fusion_race_keys)
        self._limits = limits
        self._bankroll = bankroll
        self._event_store = event_store
        self._venue = venue
        self._tournament_id = tournament_id
        self._alerter = alerter
        self._sleep = sleep
        self._daily_realized_pnl = 0.0
        self._orders: dict[str, Order] = {}
        self._halted_markets: set[str] = set()
        self._size_ramp = size_ramp
        # Venue positions, fed by reconciliation. Until the first feed,
        # fully-filled orders stand in for the positions they created.
        self._positions: list[PositionExposure] = []
        self._positions_known = False
        self._killed = False

    def record_reconciliation(self, matched: bool, detail: str = "") -> None:
        """Feed each position reconciliation result to the size ramp. The
        cancel-all / halt on mismatch is the reconciliation loop's job;
        this only moves the ramp."""
        if matched:
            self._size_ramp.record_clean_reconciliation()
        else:
            self._size_ramp.record_failure("reconciliation_mismatch", detail)

    def record_rate_limited(self, endpoint: str, retry_after_seconds: float | None = None) -> None:
        """The venue adapter calls this on every 429 it sees, including ones
        it then retries successfully. The adapter backs off its own request
        rate; the ramp only drops on a burst (SizeRamp.record_rate_limited)."""
        detail = f"429 on {endpoint}"
        if retry_after_seconds is not None:
            detail += f", Retry-After {retry_after_seconds:g}s"
        self._size_ramp.record_rate_limited(detail)

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

    def update_positions(self, positions: list[PositionExposure]) -> None:
        """Replace the position snapshot (reconciliation loop, after a clean
        comparison). From now on fully-filled orders stop counting: their
        exposure is in the positions."""
        self._positions = list(positions)
        self._positions_known = True

    def _exposure_orders(self) -> list[Order]:
        orders = self._tracked_orders()
        if self._positions_known:
            orders = [o for o in orders if o.status != OrderStatus.FILLED]
        return orders

    @property
    def is_killed(self) -> bool:
        return self._killed

    async def kill(self, reason: str) -> KillSwitchResult:
        """The kill path for the KILL file and Telegram /kill (CLAUDE.md Hard
        Rule 7). Latches a global halt first, so check() rejects every order
        from here on even if the cancel below fails, then cancels everything
        in the Cup. The halt only clears on a process restart."""
        self._killed = True
        self._event_store.log("kill", {"reason": reason})
        self._alerter.send(f"KILL ({reason}): quoting halted, cancelling all orders.")
        result = await self.kill_switch()
        if result.success:
            self._alerter.send(f"KILL ({reason}): all orders confirmed cancelled.")
        return result

    def reset_size_ramp(self, reason: str) -> None:
        """Telegram /resetramp: back to launch_fraction."""
        self._size_ramp.reset(reason)

    def is_market_halted(self, market_id: str) -> bool:
        return market_id in self._halted_markets

    def record_order_rejection(
        self, market_id: str, status_code: int, error_code: str, message: str
    ) -> None:
        """Any unexpected 4xx on an order (possible undocumented position
        limit) stops quoting in that market and alerts — never retried.
        CLAUDE.md's retry rules mean 429 and 409 REQUEST_IN_FLIGHT are
        already retried transparently at the venue-adapter layer and never
        reach here, so any call to this method is terminal by the time it
        arrives: purely a halt-and-alert step, no retry attempted.
        """
        self._halted_markets.add(market_id)
        self._event_store.log(
            "market_halted",
            {
                "market_id": market_id,
                "status_code": status_code,
                "error_code": error_code,
                "message": message,
            },
        )
        self._alerter.send(
            f"Market {market_id} halted: unexpected {status_code} {error_code} "
            f"on order placement ({message}). No retry attempted."
        )
        self._size_ramp.record_failure(
            "unexpected_4xx", f"{status_code} {error_code} on {market_id}"
        )

    def check(self, order: Order, *, fair_value: float | None, outside_data_age_seconds: float) -> RiskDecision:
        decision = self._evaluate(order, fair_value=fair_value, outside_data_age_seconds=outside_data_age_seconds)
        if not decision.approved:
            self._event_store.log(
                "risk_rejection",
                {
                    "market_id": order.market_id,
                    "exchange_id": order.exchange_id,
                    "party_id": order.party_id,
                    "race_key": order.race_key,
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

        if self._killed:
            return RiskDecision(False, "kill switch engaged")

        if order.market_id is not None and self.is_market_halted(order.market_id):
            return RiskDecision(False, "market halted after an unexpected order rejection")

        if outside_data_age_seconds > limits.stale_data_stop_seconds:
            return RiskDecision(False, "stale outside data")

        if self._bankroll > 0 and (self._daily_realized_pnl / self._bankroll) <= -limits.daily_loss_stop_fraction:
            return RiskDecision(False, "daily loss stop triggered")

        order_notional = _order_notional(order)
        # The ramp scales only order size and the per-market cap; total and
        # party exposure caps are portfolio safety limits and stay as set.
        ramp = self._size_ramp.multiplier
        ramp_note = f" (size ramp at {ramp:.0%})" if ramp < 1.0 else ""

        if order_notional > limits.max_order_size_susqies * ramp:
            return RiskDecision(False, "exceeds max order size" + ramp_note)

        if order.price is not None and fair_value is not None:
            if abs(order.price - fair_value) > limits.max_price_deviation_from_fair_value:
                return RiskDecision(False, "price deviates too far from fair value")

        tracked = self._exposure_orders()
        positions = self._positions
        market_positions = sum(p.notional for p in positions if p.market_id == order.market_id)

        market_cap = limits.max_bankroll_fraction_per_market * self._bankroll * ramp
        if _market_notional(tracked, order.market_id) + market_positions + order_notional > market_cap:
            return RiskDecision(False, "exceeds per-market bankroll cap" + ramp_note)

        if order.party_id in _RD_PARTIES and not order.race_key:
            return RiskDecision(False, "R/D order has no race_key; fusion risk can't be checked")

        if order.party_id in _RD_PARTIES and order.race_key not in self._fusion_race_keys:
            party_cap = limits.max_party_exposure_fraction * self._bankroll
            order_signed = order_notional * (1 if _is_long(order.side, order.action) else -1)
            new_net = net_rd_exposure(tracked, self._fusion_race_keys) + net_rd_positions(positions, self._fusion_race_keys) + (
                order_signed if order.party_id == "R" else -order_signed
            )
            if abs(new_net) > party_cap:
                return RiskDecision(False, "exceeds net R-vs-D party-exposure cap")

        total_cap = limits.max_total_exposure_fraction * self._bankroll
        if _total_notional(tracked) + sum(p.notional for p in positions) + order_notional > total_cap:
            return RiskDecision(False, "exceeds total exposure cap")

        return RiskDecision(True)

    # Test orders (go-live gate (c)/(d), scripts/place_test_order.py): the
    # one kind of order allowed far from fair value, so only 1 share at a
    # price no real trade happens at.
    TEST_ORDER_MAX_QUANTITY = 1
    TEST_ORDER_MAX_BUY_PRICE = 0.01
    TEST_ORDER_MIN_SELL_PRICE = 0.99

    def check_test_order(self, order: Order) -> RiskDecision:
        """Every normal check except distance from fair value (a test order
        is far from the market on purpose), plus hard limits: 1 share, a
        YES buy at <= 0.01 or a YES sell at >= 0.99. Logged either way."""
        if order.quantity > self.TEST_ORDER_MAX_QUANTITY:
            decision = RiskDecision(False, f"test orders are limited to {self.TEST_ORDER_MAX_QUANTITY} share")
        elif order.price is None or order.side != "yes" or not (
            (order.action == "buy" and order.price <= self.TEST_ORDER_MAX_BUY_PRICE)
            or (order.action == "sell" and order.price >= self.TEST_ORDER_MIN_SELL_PRICE)
        ):
            decision = RiskDecision(False, "test orders must be a YES limit at an extreme price "
                                           f"(buy <= {self.TEST_ORDER_MAX_BUY_PRICE}, sell >= {self.TEST_ORDER_MIN_SELL_PRICE})")  # fmt: skip
        else:
            decision = self._evaluate(order, fair_value=None, outside_data_age_seconds=0.0)
        self._event_store.log("test_order_check", {
            "market_id": order.market_id, "exchange_id": order.exchange_id, "action": order.action,
            "quantity": order.quantity, "price": order.price, "approved": decision.approved,
            "reason": decision.reason,
        })  # fmt: skip
        return decision

    async def kill_switch(
        self,
        exchange_id: str | None = None,
        market_id: str | None = None,
        max_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> KillSwitchResult:
        """Scoped cancel-all, then confirm via get_open_orders (our stand-in
        for GET /orders?status=open scoped to the Cup). The cancel-all
        response is never trusted alone — a venue can report success while
        an order is still actually resting (docs/platform/SUMMARY.md's
        CancelAllPausedError, or a plain bug). Any discrepancy is alerted
        immediately, not just after retries are exhausted, and retried up
        to max_attempts times.
        """
        open_orders: list[Order] = []
        for attempt in range(1, max_attempts + 1):
            await self._venue.cancel_all(
                self._tournament_id, exchange_id=exchange_id, market_id=market_id
            )
            open_orders = await self._venue.get_open_orders(
                self._tournament_id, exchange_id=exchange_id
            )
            if not open_orders:
                self._event_store.log(
                    "kill_switch",
                    {
                        "attempt": attempt,
                        "result": "clean",
                        "exchange_id": exchange_id,
                        "market_id": market_id,
                    },
                )
                return KillSwitchResult(success=True, attempts=attempt, remaining_order_ids=[])

            remaining_ids = [o.id for o in open_orders]
            self._alerter.send(
                f"Kill switch: {len(remaining_ids)} order(s) still open after "
                f"cancel-all (attempt {attempt}/{max_attempts}); retrying: {remaining_ids}"
            )
            self._event_store.log(
                "kill_switch",
                {
                    "attempt": attempt,
                    "result": "orders_remaining",
                    "remaining_order_ids": remaining_ids,
                },
            )
            if attempt < max_attempts:
                await self._sleep(retry_delay_seconds)

        remaining_ids = [o.id for o in open_orders]
        self._alerter.send(
            f"Kill switch FAILED after {max_attempts} attempts: "
            f"{len(remaining_ids)} order(s) still open: {remaining_ids}"
        )
        self._event_store.log(
            "kill_switch",
            {"attempt": max_attempts, "result": "failed", "remaining_order_ids": remaining_ids},
        )
        return KillSwitchResult(
            success=False, attempts=max_attempts, remaining_order_ids=remaining_ids
        )

    def schedule_markouts(
        self, fill: Fill, price_lookup: Callable[[int], Awaitable[float]]
    ) -> list[asyncio.Task]:
        """Log a markout to events_log at 1, 5 and 30 minutes after every
        fill. `price_lookup(minutes)` is awaited at each horizon to fetch
        the price to compare against — called with the horizon itself so
        callers don't need to track wall-clock time separately.
        """
        return [
            asyncio.create_task(self._log_markout_after(fill, price_lookup, minutes))
            for minutes in MARKOUT_HORIZONS_MINUTES
        ]

    async def _log_markout_after(
        self, fill: Fill, price_lookup: Callable[[int], Awaitable[float]], minutes: int
    ) -> None:
        await self._sleep(minutes * 60)
        later_price = await price_lookup(minutes)
        markout = compute_markout(fill, later_price)
        self._event_store.log(
            "markout",
            {
                "fill_id": fill.id,
                "exchange_id": fill.exchange_id,
                "minutes": minutes,
                "fill_price": fill.price,
                "later_price": later_price,
                "markout": markout,
            },
        )
