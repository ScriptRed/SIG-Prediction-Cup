"""Pure logic for the per-race YES-price consistency scanner: sum a race's
markets' YES prices and flag anything outside [99, 101] points. A sum far
from 100 means the Cup's books disagree with themselves about that race's
outcome probabilities — either a mispricing to trade, or (pre-launch,
seeded-but-untraded books) just noise.

The actual book fetch lives in scripts/scan_race_overround.py; this module
is the testable, side-effect-free part.
"""

from __future__ import annotations

from dataclasses import dataclass

LOW_FLAG_POINTS = 99.0
HIGH_FLAG_POINTS = 101.0


def representative_price(
    latest_price: float | None, best_bid: float | None, best_ask: float | None
) -> float | None:
    """Best single number for "this exchange's current YES price" from a
    GET /exchanges/{id}/price response. Preference: last trade > mid of
    bid/ask > whichever single side exists > None if the book is
    completely empty (no trades, no bid, no ask)."""
    if latest_price is not None:
        return latest_price
    if best_bid is not None and best_ask is not None:
        return (best_bid + best_ask) / 2
    if best_bid is not None:
        return best_bid
    if best_ask is not None:
        return best_ask
    return None


@dataclass(frozen=True)
class RaceSummary:
    race_key: str
    status: str  # "ok" / "flagged" / "insufficient_data" / "skipped_fusion"
    sum_points: float | None
    prices: dict[str, float]
    missing_parties: tuple[str, ...] = ()


def summarize_race(race_key: str, party_prices: dict[str, float | None]) -> RaceSummary:
    """party_prices maps each race's party code ("R"/"D"/"I") to its
    representative_price(), or None if that market's book is empty. A
    missing price is never treated as 0 — that would silently understate
    the sum and could mask a real overround."""
    missing = tuple(sorted(p for p, price in party_prices.items() if price is None))
    present = {p: v for p, v in party_prices.items() if v is not None}
    if missing:
        return RaceSummary(
            race_key=race_key,
            status="insufficient_data",
            sum_points=None,
            prices=present,
            missing_parties=missing,
        )
    sum_points = round(sum(present.values()) * 100, 2)
    flagged = sum_points > HIGH_FLAG_POINTS or sum_points < LOW_FLAG_POINTS
    return RaceSummary(
        race_key=race_key,
        status="flagged" if flagged else "ok",
        sum_points=sum_points,
        prices=present,
    )


def skipped_fusion(race_key: str) -> RaceSummary:
    """A fusion-risk race: a fusion candidate counts for every party on the
    ticket, so more than one party's market can resolve YES and the YES
    prices need not sum to 100. Not scanned at all."""
    return RaceSummary(race_key=race_key, status="skipped_fusion", sum_points=None, prices={})
