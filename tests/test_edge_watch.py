"""predcup/edge_watch.py: alerts when a SIG quote crosses Kalshi by
min_edge. Alerts only: the fake venue fails the test on any order or
cancel call."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from predcup.edge_watch import EdgeWatcher, EdgeWatcherConfig, find_edges, register_edge_watcher
from predcup.mapped import MappedMarket, load_mapped_markets
from predcup.models import OrderBook, OrderBookLevel
from predcup.store import EventStore
from predcup.venues.kalshi import parse_market

TID = "550e8400-e29b-41d4-a716-446655440000"
CFG = EdgeWatcherConfig(interval_seconds=15, min_edge=0.02, alert_cooldown_seconds=600, verified_only=True)


def km(ticker: str, bid: str, ask: str):
    return parse_market({"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "yes_bid_dollars": bid,
                         "yes_ask_dollars": ask, "volume_fp": "50000"})  # fmt: skip


def mm(ex: str, race: str, party: str, ticker: str, polarity: str = "same") -> MappedMarket:
    return MappedMarket(market_id=f"m{ex}", exchange_id=ex, race_key=race, party=party, title=f"{party} {race}",
                        kalshi_ticker=ticker, polarity=polarity, fusion_risk=False)  # fmt: skip


class ReadOnlyVenue:
    def __init__(self, tops: dict, books: dict) -> None:
        self.tops = tops  # exchange id -> (bid, ask)
        self.books = books  # exchange id -> OrderBook
        self.book_reads: list[str] = []

    async def get_top_of_books(self, ids, tournament_id):
        assert tournament_id == TID
        return {i: self.tops[i] for i in ids if i in self.tops}

    async def get_book(self, exchange_id, tournament_id):
        assert tournament_id == TID
        self.book_reads.append(exchange_id)
        return self.books[exchange_id]

    def __getattr__(self, name):  # place_order, place_batch, cancel, cancel_all, ...
        raise AssertionError(f"edge watcher must not call venue.{name}")


class FakeKalshi:
    def __init__(self, markets: dict | None = None, error: Exception | None = None) -> None:
        self.markets = markets or {}
        self.error = error

    async def get_markets(self, tickers):
        if self.error:
            raise self.error
        return {t: self.markets[t] for t in tickers if t in self.markets}


class Alerts:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, message: str) -> None:
        self.sent.append(message)


def book(ex: str, bids=(), asks=()) -> OrderBook:
    return OrderBook(exchange_id=ex, tournament_id=TID,
                     bids=[OrderBookLevel(price=p, quantity=q) for p, q in bids],
                     asks=[OrderBookLevel(price=p, quantity=q) for p, q in asks])  # fmt: skip


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def watcher(venue, kalshi, markets, clock=None, cfg=CFG):
    store, alerts = EventStore(":memory:"), Alerts()
    w = EdgeWatcher(venue=venue, kalshi=kalshi, store=store, alerter=alerts, tournament_id=TID,
                    markets=markets, cfg=cfg, mono=clock or Clock())  # fmt: skip
    return w, store, alerts


# --- pure edge maths ------------------------------------------------------------


def test_buy_edge_when_sig_ask_below_kalshi_bid():
    edges = find_edges(mm("1", "MI-Senate", "D", "SENATEMI-26-D"), 0.50, 0.54, km("SENATEMI-26-D", "0.57", "0.58"), 0.02)
    assert [(e.action, e.sig_price, round(e.kalshi_price, 3), round(e.edge, 3)) for e in edges] == [("buy", 0.54, 0.57, 0.03)]


def test_sell_edge_with_inverted_polarity():
    # SIG YES = Kalshi NO: Kalshi YES 0.50/0.52 -> SIG terms bid 0.48, ask 0.50.
    edges = find_edges(mm("1", "X", "D", "K-26-R", "inverted"), 0.53, 0.56, km("K-26-R", "0.50", "0.52"), 0.02)
    assert [(e.action, e.sig_price, round(e.kalshi_price, 3), round(e.edge, 3)) for e in edges] == [("sell", 0.53, 0.5, 0.03)]


def test_below_threshold_and_one_sided_books_give_no_edge():
    m = mm("1", "X", "D", "K-26-D")
    assert find_edges(m, 0.50, 0.555, km("K-26-D", "0.57", "0.58"), 0.02) == []  # 1.5 pts
    assert find_edges(m, None, 0.50, km("K-26-D", "0", "0.58"), 0.02) == []  # Kalshi has no bid
    assert find_edges(m, 0.50, 0.54, None, 0.02) == []
    assert len(find_edges(m, 0.50, 0.55, km("K-26-D", "0.57", "0.58"), 0.02)) == 1  # exactly 2.0 pts counts


# --- the watcher ----------------------------------------------------------------


def test_alert_has_race_side_price_size_and_edge_and_is_logged():
    venue = ReadOnlyVenue({"1": (0.50, 0.54)}, {"1": book("1", bids=[(0.50, 10)], asks=[(0.54, 120), (0.545, 30), (0.56, 99)])})
    w, store, alerts = watcher(venue, FakeKalshi({"SENATEMI-26-D": km("SENATEMI-26-D", "0.57", "0.58")}),
                               [mm("1", "MI-Senate", "D", "SENATEMI-26-D")])
    asyncio.run(w.run_once())
    assert len(alerts.sent) == 1
    msg = alerts.sent[0]
    for part in ("MI-Senate", "D", "buy SIG YES", "0.540", "x 120", "Kalshi bid 0.570", "3.0 pts", "150 within"):
        assert part in msg, (part, msg)
    assert "no order" in msg.lower()
    (event,) = store.all_events("edge_alert")
    assert event["payload"]["exchange_id"] == "1" and event["payload"]["action"] == "buy"
    assert event["payload"]["size"] == 120 and event["payload"]["crossing_size"] == 150


def test_no_edge_reads_no_book_and_sends_nothing():
    venue = ReadOnlyVenue({"1": (0.50, 0.56)}, {})
    w, _, alerts = watcher(venue, FakeKalshi({"K-26-D": km("K-26-D", "0.57", "0.58")}), [mm("1", "X", "D", "K-26-D")])
    asyncio.run(w.run_once())
    assert alerts.sent == [] and venue.book_reads == []


def test_edge_gone_by_book_read_is_not_alerted():
    venue = ReadOnlyVenue({"1": (0.50, 0.54)}, {"1": book("1", asks=[(0.56, 50)])})
    w, store, alerts = watcher(venue, FakeKalshi({"K-26-D": km("K-26-D", "0.57", "0.58")}), [mm("1", "X", "D", "K-26-D")])
    asyncio.run(w.run_once())
    assert alerts.sent == [] and store.all_events("edge_alert") == []


def test_alerts_are_rate_limited_per_market():
    clock = Clock()
    venue = ReadOnlyVenue(
        {"1": (0.50, 0.54), "2": (0.50, 0.54)},
        {"1": book("1", asks=[(0.54, 10)]), "2": book("2", asks=[(0.54, 10)])},
    )
    kalshi = FakeKalshi({"A-26-D": km("A-26-D", "0.57", "0.58"), "B-26-D": km("B-26-D", "0.57", "0.58")})
    w, _, alerts = watcher(venue, kalshi, [mm("1", "A", "D", "A-26-D")], clock)
    asyncio.run(w.run_once())
    clock.t += 60
    asyncio.run(w.run_once())
    assert len(alerts.sent) == 1
    w.markets.append(mm("2", "B", "D", "B-26-D"))  # another market is not held back by market 1
    asyncio.run(w.run_once())
    assert len(alerts.sent) == 2 and " B " in alerts.sent[1]
    clock.t += 600
    asyncio.run(w.run_once())
    assert len(alerts.sent) == 4


def test_read_failure_is_logged_not_raised_or_alerted():
    venue = ReadOnlyVenue({"1": (0.50, 0.54)}, {})
    w, store, alerts = watcher(venue, FakeKalshi(error=RuntimeError("kalshi down")), [mm("1", "X", "D", "K-26-D")])
    asyncio.run(w.run_once())
    assert alerts.sent == []
    assert "kalshi down" in store.all_events("edge_watch_failed")[0]["payload"]["error"]


# --- mapped markets ---------------------------------------------------------------

CUP = [
    {"id": "1", "exchange_id": "11", "title": "t1", "party": "D", "race_key": "A"},
    {"id": "2", "exchange_id": "12", "title": "t2", "party": "R", "race_key": "A"},
    {"id": "3", "exchange_id": "13", "title": "t3", "party": "D", "race_key": "B"},
    {"id": "4", "exchange_id": "14", "title": "t4", "party": "D", "race_key": "C"},
]


def row(pid, ticker, verified="true", polarity="same", fusion="false"):
    return {"platform_id": pid, "kalshi_ticker": ticker, "polarity": polarity, "verified": verified,
            "tier": "A", "fusion_risk": fusion}  # fmt: skip


def test_load_mapped_markets_filters_unverified_and_unmapped():
    rows = [row("1", "A-26-D"), row("2", "A-26-R", verified="false"), row("3", ""), row("4", "C-26-D", polarity="?")]
    assert [m.exchange_id for m in load_mapped_markets(CUP, rows, verified_only=True)] == ["11"]
    assert [m.exchange_id for m in load_mapped_markets(CUP, rows, verified_only=False)] == ["11", "12"]


# --- registration and read-only guarantee ---------------------------------------------


class FakeApp:
    def __init__(self) -> None:
        self.venue, self.kalshi, self.store, self.alerter, self.tid = object(), object(), EventStore(":memory:"), Alerts(), TID
        self.tasks: list = []
        self.periodic: list = []

    def add_task(self, factory) -> None:
        self.tasks.append(factory)

    async def _periodic(self, name, interval, step) -> None:
        self.periodic.append((name, interval, step))


SETTINGS_ON = {"edge_watcher": {"enabled": True, "interval_seconds": 15, "min_edge": 0.02,
                                "alert_cooldown_seconds": 600, "verified_only": True}}  # fmt: skip


def test_register_is_off_unless_enabled():
    app = FakeApp()
    assert register_edge_watcher(app, {"edge_watcher": {**SETTINGS_ON["edge_watcher"], "enabled": False}}, CUP, [row("1", "A-26-D")]) is None
    assert register_edge_watcher(app, {}, CUP, [row("1", "A-26-D")]) is None
    assert app.tasks == []


def test_register_adds_one_periodic_task_when_enabled():
    app = FakeApp()
    w = register_edge_watcher(app, SETTINGS_ON, CUP, [row("1", "A-26-D")])
    assert w is not None and len(app.tasks) == 1
    asyncio.run(app.tasks[0]())
    assert app.periodic[0][:2] == ("edge_watcher", 15.0)


def test_settings_yaml_ships_with_edge_watcher_disabled():
    import yaml

    settings = yaml.safe_load(Path("config/settings.yaml").read_text())
    assert settings["edge_watcher"]["enabled"] is False
    assert settings["edge_watcher"]["min_edge"] == 0.02


@pytest.mark.parametrize("path", ["predcup/edge_watch.py", "predcup/mapped.py"])
def test_module_never_touches_orders(path):
    src = Path(path).read_text()
    for forbidden in ("place_order", "place_batch", "cancel", "OrderRouter", "risk.check"):
        assert forbidden not in src, f"{path} mentions {forbidden}"
