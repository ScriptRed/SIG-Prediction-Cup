"""Markouts at 1/5/30 minutes after every fill, bot or manual, logged to
events_log from the first trade (2026-10-01). Horizons are measured from
the fill's own time: reconciliation may only see a fill up to a minute
later. The later price is the SIG mid for that exchange."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from _helpers import full_size_ramp
from predcup.control import TradingControl
from predcup.models import Fill
from predcup.reconcile import Reconciler
from predcup.risk import RiskLimits, RiskManager
from predcup.store import EventStore
from sim.mock_exchange import MockExchange

TID = "550e8400-e29b-41d4-a716-446655440000"
NOW = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)


class Alerts:
    def send(self, m):
        pass


class FakeRouter:
    def swept_snapshot(self):
        return []

    def release_swept(self, keys):
        pass

    def clear_block(self, reason):
        pass


def make(tmp_path, venue):
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    store = EventStore(tmp_path / "e.db")
    risk = RiskManager(
        limits=RiskLimits(max_bankroll_fraction_per_market=1, max_total_exposure_fraction=1,
                          max_party_exposure_fraction=1, max_order_size_susqies=1e9,
                          max_price_deviation_from_fair_value=1, daily_loss_stop_fraction=1,
                          stale_data_stop_seconds=60),  # fmt: skip
        bankroll=100_000.0, event_store=store, venue=venue, tournament_id=TID, alerter=Alerts(),
        size_ramp=full_size_ramp(), fusion_race_keys=frozenset(), sleep=fake_sleep, now=lambda: NOW,
    )  # fmt: skip
    rec = Reconciler(venue=venue, store=store, risk=risk, router=FakeRouter(), control=TradingControl(),
                     alerter=Alerts(), tournament_id=TID, market_meta={}, shadow=True)  # fmt: skip
    return rec, risk, store, sleeps


def fill(i="1", side="yes", price=0.40, at=NOW, qty=10) -> Fill:
    return Fill(id=i, order_id="", exchange_id="1068", tournament_id=TID, side=side, action="buy",
                quantity=qty, price=price, filled_at=at)  # fmt: skip


def test_schedule_markouts_measures_from_the_fill_time(tmp_path):
    _, risk, store, sleeps = make(tmp_path, MockExchange())

    async def go():
        async def lookup(minutes):
            return 0.45

        await asyncio.gather(*risk.schedule_markouts(fill(at=NOW - timedelta(seconds=45)), lookup))

    asyncio.run(go())
    assert sorted(sleeps) == [15.0, 255.0, 1755.0]  # 1, 5, 30 min after the fill, seen 45 s late
    ev = sorted(store.all_events("markout"), key=lambda e: e["payload"]["minutes"])
    assert [(e["payload"]["minutes"], e["payload"]["markout"]) for e in ev] == [
        (1, pytest.approx(0.05)), (5, pytest.approx(0.05)), (30, pytest.approx(0.05))]


def test_markout_with_no_price_is_logged_as_unavailable(tmp_path):
    _, risk, store, _ = make(tmp_path, MockExchange())

    async def go():
        async def lookup(minutes):
            return None

        await asyncio.gather(*risk.schedule_markouts(fill(), lookup))

    asyncio.run(go())
    assert store.all_events("markout") == []
    assert len(store.all_events("markout_unavailable")) == 3


def test_every_new_fill_including_manual_gets_markouts_at_sig_mid(tmp_path):
    venue = MockExchange()
    venue.set_top_of_book("1068", 0.44, 0.46)
    rec, risk, store, sleeps = make(tmp_path, venue)
    placed = asyncio.run(venue.place_order(
        __import__("predcup.models", fromlist=["Order"]).Order(
            exchange_id="1068", tournament_id=TID, side="yes", action="buy", quantity=10, price=0.40,
            idempotency_key="by-hand")))  # a manual order: the bot never saw it  # fmt: skip

    async def go():
        await venue.simulate_fill(placed.id, 10)
        await rec.run_once()
        await asyncio.gather(*rec.pending_markouts())
        await rec.run_once()  # same fill again: no second set of markouts
        await asyncio.gather(*rec.pending_markouts())

    asyncio.run(go())
    ev = store.all_events("markout")
    assert sorted(e["payload"]["minutes"] for e in ev) == [1, 5, 30]
    assert all(e["payload"]["later_price"] == pytest.approx(0.45) for e in ev)
    assert all(e["payload"]["markout"] == pytest.approx(0.05) for e in ev)


def test_one_sided_book_uses_no_price(tmp_path):
    venue = MockExchange()
    venue.set_top_of_book("1068", None, 0.46)
    rec, risk, store, _ = make(tmp_path, venue)

    async def go():
        lookup = rec.price_lookup("1068")
        return await lookup(1)

    assert asyncio.run(go()) is None


# --- audit 2026-10-01 H4: reconciliation feeds day P&L and account value into risk ------

from predcup.models import Order  # noqa: E402
from predcup.venues.sig import TournamentPnl  # noqa: E402


class PnlMock(MockExchange):
    def __init__(self, period_pnl, account_value=100_000.0, fail=False):
        super().__init__()
        self.period_pnl, self.account_value, self.fail = period_pnl, account_value, fail

    async def get_pnl(self, tournament_id, period):
        if self.fail:
            raise ConnectionError("pnl read failed")
        assert period == "day"
        return TournamentPnl(period="day", period_pnl=self.period_pnl, unrealized_pnl=0.0,
                             total_account_value=self.account_value, roi=None)  # fmt: skip


def _bot_order():
    return Order(exchange_id="1068", market_id="379", tournament_id=TID, party_id="D", race_key="MA-Senate",
                 side="yes", action="buy", quantity=10, price=0.5, idempotency_key="b")  # fmt: skip


def _real_limits_risk(tmp_path, venue):
    rec, risk, store, _ = make(tmp_path, venue)
    risk._limits = risk._limits.__class__(**{**risk._limits.__dict__, "daily_loss_stop_fraction": 0.08})
    return rec, risk, store


def test_day_loss_beyond_the_stop_blocks_orders(tmp_path):
    rec, risk, store = _real_limits_risk(tmp_path, PnlMock(period_pnl=-9_000.0, account_value=91_000.0))
    assert risk.check(_bot_order(), fair_value=0.5, outside_data_age_seconds=0).approved
    asyncio.run(rec.run_once())
    d = risk.check(_bot_order(), fair_value=0.5, outside_data_age_seconds=0)
    assert not d.approved and d.reason == "daily loss stop triggered"


def test_account_value_becomes_the_bankroll(tmp_path):
    rec, risk, _ = _real_limits_risk(tmp_path, PnlMock(period_pnl=1_000.0, account_value=101_000.0))
    asyncio.run(rec.run_once())
    assert risk._bankroll == 101_000.0 and risk._daily_realized_pnl == 1_000.0


def test_null_day_pnl_changes_nothing_but_bankroll(tmp_path):
    rec, risk, _ = _real_limits_risk(tmp_path, PnlMock(period_pnl=None, account_value=99_000.0))
    asyncio.run(rec.run_once())
    assert risk._daily_realized_pnl == 0.0 and risk._bankroll == 99_000.0


def test_pnl_read_failure_is_logged_not_a_reconciliation_failure(tmp_path):
    rec, risk, store = _real_limits_risk(tmp_path, PnlMock(period_pnl=0.0, fail=True))
    assert asyncio.run(rec.run_once()).status == "clean"
    assert store.all_events("pnl_read_failed")
    assert risk._bankroll == 100_000.0


# --- 2026-10-01 live: the DE-Senate R fill (1,000 NO at 0.920) showed a +0.423 markout --------
# Raw fill: {"exchangeId": "1075", "price": 0.92, "quantity": -1000, "side": "no"}.
# 0.92 is the NO price; stored as if YES-normalized and marked against the
# mid of a hollow SIG book (bid 0.01, ask 0.97 -> ~0.49) it gave +0.423.


def test_sig_no_side_fill_price_is_stored_yes_normalized():
    import httpx

    from predcup.venues.sig import SigVenue

    raw = {"id": 1833391, "orderId": 732303, "exchangeId": "1075", "marketId": "386", "price": 0.92,
           "quantity": -1000, "side": "no", "filledAt": "2026-10-01T16:05:59.180Z"}  # fmt: skip

    def handler(request):
        if request.url.path.endswith("/tournaments/cup"):
            return httpx.Response(200, json={"id": TID})
        return httpx.Response(200, json={"data": [raw], "pagination": {"limit": 200, "hasMore": False, "nextCursor": None}})

    venue = SigVenue(httpx.AsyncClient(transport=httpx.MockTransport(handler)), base_url="https://sig.test/api/v1",
                     api_key="k", tournament_slug="cup", on_rate_limited=lambda e, r: None)  # fmt: skip
    [f] = asyncio.run(venue.get_new_fills(TID, known_ids=set()))
    assert (f.side, f.action, f.quantity) == ("no", "buy", 1000)
    assert f.price == pytest.approx(0.08)  # YES terms: sold YES at 0.08


def _de_fill():
    return Fill(id="1833391", order_id="732303", exchange_id="1075", tournament_id=TID, side="no", action="buy",
                quantity=1000, price=0.08, filled_at=NOW)  # fmt: skip


def _lookup_markouts(tmp_path, book, fair_value=None):
    venue = MockExchange()
    venue.set_top_of_book("1075", *book)
    rec, risk, store, _ = make(tmp_path, venue)
    rec._fair_value_of = lambda ex: fair_value

    async def go():
        await asyncio.gather(*risk.schedule_markouts(_de_fill(), rec.price_lookup("1075")))

    asyncio.run(go())
    return store


def test_hollow_sig_book_gives_markout_unavailable_not_a_number(tmp_path):
    store = _lookup_markouts(tmp_path, (0.01, 0.97))  # spread 96 points, mid ~0.49
    assert store.all_events("markout") == []
    assert len(store.all_events("markout_unavailable")) == 3


def test_tight_sig_book_mid_is_used_when_no_fair_value(tmp_path):
    store = _lookup_markouts(tmp_path, (0.01, 0.03))  # spread 2 points, mid 0.02
    ev = store.all_events("markout")
    assert {e["payload"]["source"] for e in ev} == {"sig_mid"}
    assert all(e["payload"]["markout"] == pytest.approx(0.06) for e in ev)  # short YES at 0.08, now 0.02


def test_spread_of_exactly_five_points_is_not_under_five(tmp_path):
    store = _lookup_markouts(tmp_path, (0.10, 0.15))
    assert store.all_events("markout") == []


def test_kalshi_fair_value_is_preferred_over_sig_mid(tmp_path):
    store = _lookup_markouts(tmp_path, (0.01, 0.03), fair_value=0.10)  # polarity-adjusted, SIG YES terms
    ev = store.all_events("markout")
    assert {e["payload"]["source"] for e in ev} == {"kalshi_fair_value"}
    assert all(e["payload"]["later_price"] == pytest.approx(0.10) for e in ev)
    assert all(e["payload"]["markout"] == pytest.approx(-0.02) for e in ev)


def test_app_feeds_fresh_fair_values_to_markouts(tmp_path):
    from datetime import timedelta

    from predcup.fairvalue import FairValue
    from test_app import make_app

    app, _ = make_app(tmp_path, clock=lambda: NOW)
    app.fair_values._current["1068"] = FairValue(ok=True, value=0.61, uncertainty=0.01, as_of=NOW)
    app.fair_values._current["1059"] = FairValue(ok=True, value=0.3, uncertainty=0.01, as_of=NOW - timedelta(seconds=120))
    assert app.reconciler._fair_value_of("1068") == 0.61
    assert app.reconciler._fair_value_of("1059") is None  # stale
    assert app.reconciler._fair_value_of("9999") is None  # no Kalshi mapping
