"""Edge watcher: alerts only, never orders (CLAUDE.md Hard Rules 1-2).

Every `interval_seconds`, read the SIG top of book and the Kalshi quote for
each mapped market and alert on Telegram when a SIG quote crosses Kalshi by
at least `min_edge` in SIG YES terms (polarity applied):
- buy:  SIG ask <= Kalshi bid - min_edge (buy SIG YES below where Kalshi bids)
- sell: SIG bid >= Kalshi ask + min_edge (sell SIG YES above where Kalshi offers)
A candidate is re-read from the SIG order book for the size before it is
alerted, and alerts are rate-limited per market (`alert_cooldown_seconds`).
Each alert is logged to events_log as `edge_alert`; a failed read is logged
as `edge_watch_failed` and never alerted (kalshi_poll already reports
outages).

Off by default (settings edge_watcher.enabled). Its SIG reads share the
account's rate limit: a 429 here reaches the size ramp like any other.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from predcup.launch_report import kalshi_quotes_in_sig_terms
from predcup.mapped import MappedMarket, load_mapped_markets, read_both
from predcup.models import OrderBook
from predcup.store import EventStore
from predcup.venues.kalshi import KalshiMarket

_EPS = 1e-9


@dataclass(frozen=True)
class EdgeWatcherConfig:
    interval_seconds: float
    min_edge: float  # SIG quote beyond Kalshi's opposite side, in price (0.02 = 2 points)
    alert_cooldown_seconds: float  # per market
    verified_only: bool


def load_edge_watcher_config(settings: dict) -> EdgeWatcherConfig:
    e = settings["edge_watcher"]
    return EdgeWatcherConfig(
        interval_seconds=float(e["interval_seconds"]),
        min_edge=float(e["min_edge"]),
        alert_cooldown_seconds=float(e["alert_cooldown_seconds"]),
        verified_only=bool(e["verified_only"]),
    )


@dataclass(frozen=True)
class Edge:
    market: MappedMarket
    action: str  # "buy" | "sell" SIG YES
    sig_price: float
    kalshi_price: float  # Kalshi bid (buy) / ask (sell), in SIG YES terms
    edge: float


def find_edges(
    m: MappedMarket, sig_bid: float | None, sig_ask: float | None, kalshi: KalshiMarket | None, min_edge: float
) -> list[Edge]:
    if kalshi is None:
        return []
    k_bid, k_ask = kalshi_quotes_in_sig_terms(kalshi, m.polarity)
    out = []
    if sig_ask is not None and k_bid is not None and k_bid - sig_ask >= min_edge - _EPS:
        out.append(Edge(m, "buy", sig_ask, k_bid, k_bid - sig_ask))
    if sig_bid is not None and k_ask is not None and sig_bid - k_ask >= min_edge - _EPS:
        out.append(Edge(m, "sell", sig_bid, k_ask, sig_bid - k_ask))
    return out


def _sizes(edge: Edge, book: OrderBook, min_edge: float) -> tuple[float, float, float] | None:
    """(best price, size at it, size at every level still crossing by
    min_edge) on the side the edge would hit, or None if it no longer crosses."""
    if edge.action == "buy":
        levels = [lv for lv in book.asks if edge.kalshi_price - lv.price >= min_edge - _EPS]
        levels.sort(key=lambda lv: lv.price)
    else:
        levels = [lv for lv in book.bids if lv.price - edge.kalshi_price >= min_edge - _EPS]
        levels.sort(key=lambda lv: -lv.price)
    if not levels:
        return None
    best = levels[0].price
    at_best = sum(lv.quantity for lv in levels if abs(lv.price - best) < _EPS)
    return best, at_best, sum(lv.quantity for lv in levels)


class EdgeWatcher:
    def __init__(
        self,
        *,
        venue,
        kalshi,
        store: EventStore,
        alerter,
        tournament_id: str,
        markets: list[MappedMarket],
        cfg: EdgeWatcherConfig,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self.venue = venue
        self.kalshi = kalshi
        self.store = store
        self.alerter = alerter
        self.tid = tournament_id
        self.markets = markets
        self.cfg = cfg
        self.mono = mono
        self._last_alert: dict[str, float] = {}  # exchange id -> mono time

    def _cooling(self, exchange_id: str, now: float) -> bool:
        last = self._last_alert.get(exchange_id)
        return last is not None and now - last < self.cfg.alert_cooldown_seconds

    async def run_once(self) -> list[Edge]:
        if not self.markets:
            return []
        try:
            sig, kalshi = await read_both(self.venue, self.kalshi, self.tid, self.markets)
        except Exception as e:
            self.store.log("edge_watch_failed", {"stage": "read", "error": repr(e)[:300]})
            return []
        alerted = []
        for m in self.markets:
            now = self.mono()
            if self._cooling(m.exchange_id, now):
                continue
            bid, ask = sig.get(m.exchange_id, (None, None))
            edges = find_edges(m, bid, ask, kalshi.get(m.kalshi_ticker), self.cfg.min_edge)
            if not edges:
                continue
            try:
                book = await self.venue.get_book(m.exchange_id, self.tid)
            except Exception as e:
                self.store.log("edge_watch_failed", {"stage": "book", "exchange_id": m.exchange_id,
                                                     "error": repr(e)[:300]})  # fmt: skip
                continue
            for edge in edges:
                sizes = _sizes(edge, book, self.cfg.min_edge)
                if sizes is None:
                    continue
                price, size, crossing = sizes
                self._alert(edge, price, size, crossing)
                self._last_alert[m.exchange_id] = now
                alerted.append(edge)
                break  # one alert per market per cooldown
        return alerted

    def _alert(self, edge: Edge, price: float, size: float, crossing: float) -> None:
        m = edge.market
        e = (edge.kalshi_price - price) if edge.action == "buy" else (price - edge.kalshi_price)
        k_side = "bid" if edge.action == "buy" else "ask"
        sig_side = "ask" if edge.action == "buy" else "bid"
        msg = (
            f"EDGE {m.race_key} {m.party} ({m.title}): {edge.action} SIG YES at {sig_side} {price:.3f} x {size:g}"
            f" vs Kalshi {k_side} {edge.kalshi_price:.3f} ({m.kalshi_ticker}, {m.polarity}) = {e * 100:.1f} pts;"
            f" {crossing:g} within {self.cfg.min_edge * 100:g} pts."
            + (" FUSION RISK race." if m.fusion_risk else "")
            + " Alert only, no order placed."
        )
        self.store.log("edge_alert", {
            "exchange_id": m.exchange_id, "market_id": m.market_id, "race_key": m.race_key, "party": m.party,
            "action": edge.action, "price": price, "size": size, "crossing_size": crossing,
            "kalshi_ticker": m.kalshi_ticker, "kalshi_price": edge.kalshi_price, "edge": e,
        })  # fmt: skip
        self.alerter.send(msg)


def register_edge_watcher(
    app, settings: dict, cup_rows: list[dict[str, str]], map_rows: list[dict[str, str]]
) -> EdgeWatcher | None:
    """Add the watcher to `app` as a periodic task (loop lag, watchdog beat
    and loop-error handling via App._periodic) when edge_watcher.enabled."""
    if not settings.get("edge_watcher", {}).get("enabled", False):
        return None
    cfg = load_edge_watcher_config(settings)
    w = EdgeWatcher(
        venue=app.venue, kalshi=app.kalshi, store=app.store, alerter=app.alerter, tournament_id=app.tid,
        markets=load_mapped_markets(cup_rows, map_rows, verified_only=cfg.verified_only), cfg=cfg,
    )  # fmt: skip
    app.add_task(lambda: app._periodic("edge_watcher", cfg.interval_seconds, w.run_once))
    return w
