"""Fair value v1: the Kalshi mid, for verified Tier A rows only, with an
optional Polymarket blend (fair_value.use_polymarket, off by default).

CLAUDE.md "Key logic": no external match -> no automatic trading; a
market_map.csv row with verified=false counts as no match however high its
confidence. Kalshi is the anchor. Resolution adjustments and manual
override come later (PLAN steps 5/T2).

Every "no" is explicit: `FairValue.ok` False with a reason, value None.
Callers must treat that as "do not quote", never as a price.

Uncertainty = Kalshi half-spread + base + staleness (linear up to the age
limit) + a thin-book penalty. The quoter's half-spread is
max(min_edge, c * uncertainty).

Polymarket blend (`fair_value`, only when use_polymarket is true): needs a
Kalshi fair value first, so every gate above still applies and Polymarket
alone never gives a price. Polymarket's uncertainty is built the same way
from its CLOB book (thin = shares at best bid + best ask below
thin_depth). Weights are 1/u^2 per venue, so the tighter, deeper, fresher
book counts more ("liquidity-weighted"); the value is the weighted mean in
log-odds space. Uncertainty = weighted mean of the venues' u (not the
inverse-variance shrink: the two venues are far from independent) +
disagreement_factor * |kalshi - polymarket|; disagreement beyond
max_disagreement gives no fair value (a mapping error looks like this).
Polymarket unusable (no token, stale, one-sided, crossed, wide, or a
polarity other than "same") -> exactly the Kalshi fair value. With
use_polymarket off, `fair_value` returns `kalshi_fair_value` untouched.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from predcup.store import EventStore
from predcup.venues.kalshi import KalshiMarket
from predcup.venues.polymarket import PolymarketBook


@dataclass(frozen=True)
class PolymarketFairValueConfig:
    max_spread: float  # wider Polymarket spread -> not blended
    base_uncertainty: float
    thin_depth: float  # shares at best bid + best ask below this adds thin_penalty
    thin_penalty: float
    stale_penalty: float  # added linearly from 0 (fresh) to this (at max age)
    disagreement_factor: float  # uncertainty += this * |kalshi - polymarket|
    max_disagreement: float  # larger gap -> no fair value


@dataclass(frozen=True)
class FairValueConfig:
    max_age_seconds: float  # older Kalshi data -> no fair value (60 s stale-data stop)
    max_spread: float  # wider Kalshi spread -> no fair value
    base_uncertainty: float
    thin_volume: float  # Kalshi lifetime volume below this adds thin_penalty
    thin_penalty: float
    stale_penalty: float  # added linearly from 0 (fresh) to this (at max age)
    min_confidence: float  # market_map.csv confidence floor
    use_polymarket: bool = False
    polymarket: PolymarketFairValueConfig | None = None


def load_fair_value_config(settings: dict) -> FairValueConfig:
    fv = settings["fair_value"]
    k = fv["kalshi"]
    use_poly = fv.get("use_polymarket", False)
    if not isinstance(use_poly, bool):
        raise ValueError(f"fair_value.use_polymarket must be true or false, got {use_poly!r}")
    p = fv.get("polymarket")
    if use_poly and not p:
        raise ValueError("fair_value.use_polymarket is true but fair_value.polymarket is missing")
    poly = PolymarketFairValueConfig(**{f: float(p[f]) for f in PolymarketFairValueConfig.__dataclass_fields__}) if p else None
    return FairValueConfig(
        max_age_seconds=float(fv["max_outside_data_age_seconds"]),
        max_spread=float(k["max_spread"]),
        base_uncertainty=float(k["base_uncertainty"]),
        thin_volume=float(k["thin_volume"]),
        thin_penalty=float(k["thin_penalty"]),
        stale_penalty=float(k["stale_penalty"]),
        min_confidence=float(fv["min_confidence_to_trade"]),
        use_polymarket=use_poly,
        polymarket=poly,
    )


@dataclass(frozen=True)
class KalshiQuote:
    market: KalshiMarket
    fetched_at: datetime  # UTC, when our GET returned


@dataclass(frozen=True)
class PolymarketQuote:
    book: PolymarketBook  # CLOB book of the row's poly_token_id (the YES token)
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


def _polymarket_component(
    map_row: dict[str, str], quote: PolymarketQuote | None, now: datetime, cfg: FairValueConfig
) -> tuple[float, float] | None:
    """(value, uncertainty) from Polymarket, or None when it can't be used."""
    p = cfg.polymarket
    token = map_row.get("poly_token_id", "")
    if p is None or not token or quote is None or quote.book.token_id != token:
        return None
    if map_row.get("polarity", "") != "same":
        return None
    age = (now - quote.fetched_at).total_seconds()
    b = quote.book
    if age > cfg.max_age_seconds or b.best_bid is None or b.best_ask is None:
        return None
    spread = b.best_ask - b.best_bid
    if spread < 0 or spread > p.max_spread:
        return None
    u = spread / 2 + p.base_uncertainty + p.stale_penalty * max(0.0, age) / cfg.max_age_seconds
    if (b.best_bid_size or 0) + (b.best_ask_size or 0) < p.thin_depth:
        u += p.thin_penalty
    return (b.best_bid + b.best_ask) / 2, u


def _logit(x: float) -> float:
    x = min(max(x, 1e-6), 1 - 1e-6)
    return math.log(x / (1 - x))


def fair_value(
    map_row: dict[str, str],
    kalshi: KalshiQuote | None,
    polymarket: PolymarketQuote | None,
    now: datetime,
    cfg: FairValueConfig,
) -> FairValue:
    """Kalshi fair value, blended with Polymarket when cfg.use_polymarket.
    Off -> exactly kalshi_fair_value."""
    k = kalshi_fair_value(map_row, kalshi, now, cfg)
    if not cfg.use_polymarket or not k.ok:
        return k
    poly = _polymarket_component(map_row, polymarket, now, cfg)
    if poly is None:
        return k
    assert cfg.polymarket is not None and polymarket is not None and k.value is not None and k.uncertainty is not None
    p_value, p_u = poly
    gap = abs(k.value - p_value)
    if gap > cfg.polymarket.max_disagreement:
        return FairValue.none(f"Kalshi {k.value:.3f} and Polymarket {p_value:.3f} disagree by {gap:.3f}")
    w_k, w_p = 1 / k.uncertainty**2, 1 / p_u**2
    x = (w_k * _logit(k.value) + w_p * _logit(p_value)) / (w_k + w_p)
    value = 1 / (1 + math.exp(-x))
    uncertainty = (w_k * k.uncertainty + w_p * p_u) / (w_k + w_p) + cfg.polymarket.disagreement_factor * gap
    as_of = min(k.as_of, polymarket.fetched_at) if k.as_of else polymarket.fetched_at
    return FairValue(ok=True, value=value, uncertainty=uncertainty, source="kalshi+polymarket", as_of=as_of)


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
