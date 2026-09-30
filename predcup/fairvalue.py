"""Fair value v1: the Kalshi mid, for verified Tier A rows only.

CLAUDE.md "Key logic": no external match -> no automatic trading; a
market_map.csv row with verified=false counts as no match however high its
confidence. v1 has one source (Kalshi). The Polymarket blend, resolution
adjustments and manual override come later (PLAN steps 5/T2).

Every "no" is explicit: `FairValue.ok` False with a reason, value None.
Callers must treat that as "do not quote", never as a price.

Uncertainty = Kalshi half-spread + base + staleness (linear up to the age
limit) + a thin-book penalty. The quoter's half-spread is
max(min_edge, c * uncertainty).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from predcup.store import EventStore
from predcup.venues.kalshi import KalshiMarket


@dataclass(frozen=True)
class FairValueConfig:
    max_age_seconds: float  # older Kalshi data -> no fair value (60 s stale-data stop)
    max_spread: float  # wider Kalshi spread -> no fair value
    base_uncertainty: float
    thin_volume: float  # Kalshi lifetime volume below this adds thin_penalty
    thin_penalty: float
    stale_penalty: float  # added linearly from 0 (fresh) to this (at max age)
    min_confidence: float  # market_map.csv confidence floor


def load_fair_value_config(settings: dict) -> FairValueConfig:
    fv = settings["fair_value"]
    k = fv["kalshi"]
    return FairValueConfig(
        max_age_seconds=float(fv["max_outside_data_age_seconds"]),
        max_spread=float(k["max_spread"]),
        base_uncertainty=float(k["base_uncertainty"]),
        thin_volume=float(k["thin_volume"]),
        thin_penalty=float(k["thin_penalty"]),
        stale_penalty=float(k["stale_penalty"]),
        min_confidence=float(fv["min_confidence_to_trade"]),
    )


@dataclass(frozen=True)
class KalshiQuote:
    market: KalshiMarket
    fetched_at: datetime  # UTC, when our GET returned


@dataclass(frozen=True)
class FairValue:
    ok: bool
    value: float | None  # SIG YES terms, in [0, 1]
    uncertainty: float | None
    reason: str = ""
    source: str = "kalshi"
    as_of: datetime | None = None

    @classmethod
    def none(cls, reason: str) -> FairValue:
        return cls(ok=False, value=None, uncertainty=None, reason=reason)


def kalshi_fair_value(
    map_row: dict[str, str],
    quote: KalshiQuote | None,
    now: datetime,
    cfg: FairValueConfig,
) -> FairValue:
    if map_row.get("verified", "").strip().lower() != "true":
        return FairValue.none("mapping not verified")
    if map_row.get("tier", "").strip().upper() != "A":
        return FairValue.none("not Tier A")
    if not map_row.get("kalshi_ticker"):
        return FairValue.none("no Kalshi ticker")
    polarity = map_row.get("polarity", "")
    if polarity not in ("same", "inverted"):
        return FairValue.none(f"polarity {polarity!r} unknown")
    try:
        confidence = float(map_row.get("confidence") or 0)
    except ValueError:
        return FairValue.none("confidence unreadable")
    if confidence < cfg.min_confidence:
        return FairValue.none(f"confidence {confidence} below {cfg.min_confidence}")

    if quote is None:
        return FairValue.none("no Kalshi quote")
    age = (now - quote.fetched_at).total_seconds()
    if age > cfg.max_age_seconds:
        return FairValue.none(f"stale Kalshi quote ({age:.0f}s > {cfg.max_age_seconds:.0f}s)")
    k = quote.market
    if k.yes_bid is None or k.yes_ask is None:
        return FairValue.none("Kalshi book not two-sided")
    spread = k.yes_ask - k.yes_bid
    if spread < 0:
        return FairValue.none("Kalshi book crossed")
    if spread > cfg.max_spread:
        return FairValue.none(f"Kalshi spread {spread:.3f} > {cfg.max_spread}")

    mid = (k.yes_bid + k.yes_ask) / 2
    value = mid if polarity == "same" else 1 - mid
    uncertainty = spread / 2 + cfg.base_uncertainty + cfg.stale_penalty * max(0.0, age) / cfg.max_age_seconds
    if k.volume < cfg.thin_volume:
        uncertainty += cfg.thin_penalty
    return FairValue(ok=True, value=value, uncertainty=uncertainty, as_of=quote.fetched_at)


class FairValueTracker:
    """Latest fair value per SIG exchange; logs every change (value moved by
    at least `min_change`, or availability flipped) to events_log."""

    def __init__(self, store: EventStore, min_change: float = 0.001) -> None:
        self._store = store
        self._min_change = min_change
        self._current: dict[str, FairValue] = {}

    def current(self, exchange_id: str) -> FairValue:
        return self._current.get(exchange_id, FairValue.none("no fair value yet"))

    def update(self, exchange_id: str, fv: FairValue) -> None:
        prev = self._current.get(exchange_id)
        self._current[exchange_id] = fv
        changed = (
            prev is None
            or prev.ok != fv.ok
            or (fv.ok and prev.value is not None and abs(fv.value - prev.value) >= self._min_change)  # type: ignore[operator]
        )
        if changed:
            self._store.log("fair_value", {
                "exchange_id": exchange_id,
                "value": fv.value,
                "uncertainty": fv.uncertainty,
                "source": fv.source,
                "reason": fv.reason,
                "as_of": fv.as_of.isoformat() if fv.as_of else None,
            })  # fmt: skip
