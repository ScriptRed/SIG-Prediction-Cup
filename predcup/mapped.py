"""SIG markets with a Kalshi anchor, for the read-only watchers
(predcup/edge_watch.py, predcup/seed_lag.py), and one paired read of both
venues. GET requests only: these modules never trade.
"""

from __future__ import annotations

from dataclasses import dataclass

from predcup.venues.kalshi import KalshiMarket


@dataclass(frozen=True)
class MappedMarket:
    market_id: str
    exchange_id: str
    race_key: str
    party: str
    title: str
    kalshi_ticker: str
    polarity: str  # "same" | "inverted"
    fusion_risk: bool


def load_mapped_markets(
    cup_rows: list[dict[str, str]], map_rows: list[dict[str, str]], *, verified_only: bool
) -> list[MappedMarket]:
    """Cup markets whose market_map.csv row has a Kalshi ticker and a known
    polarity (and verified=true when `verified_only`), in Cup file order."""
    by_id = {r["platform_id"]: r for r in map_rows}
    out = []
    for c in cup_rows:
        r = by_id.get(c["id"])
        if not r or not r.get("kalshi_ticker") or r.get("polarity") not in ("same", "inverted"):
            continue
        if verified_only and (r.get("verified") or "").strip().lower() != "true":
            continue
        out.append(MappedMarket(
            market_id=c["id"], exchange_id=c["exchange_id"], race_key=c["race_key"], party=c["party"],
            title=c.get("title", ""), kalshi_ticker=r["kalshi_ticker"], polarity=r["polarity"],
            fusion_risk=(r.get("fusion_risk") or "").strip().lower() == "true",
        ))  # fmt: skip
    return out


async def read_both(
    venue, kalshi, tournament_id: str, markets: list[MappedMarket]
) -> tuple[dict[str, tuple[float | None, float | None]], dict[str, KalshiMarket]]:
    """(SIG best bid/ask by exchange id, Kalshi market by ticker): one
    GET /exchanges/prices per 100 markets (Cup tournamentId) and one Kalshi
    GET /markets per 50 tickers."""
    sig = await venue.get_top_of_books([m.exchange_id for m in markets], tournament_id)
    k = await kalshi.get_markets(sorted({m.kalshi_ticker for m in markets}))
    return sig, k
