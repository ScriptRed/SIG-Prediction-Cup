"""predcup/pnl.py PnlFeed (2026-10-01): the daily loss stop's P&L, in its
own task. SIG's portfolio P&L read can take 0.2-18.5 s under load, so
between good reads the day P&L is estimated locally from our fills and
marks (Kalshi fair value, else a tight SIG mid). Orders are blocked (live)
only if neither SIG's P&L nor the estimate is available."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from _helpers import full_size_ramp
from predcup.models import Fill, Order
from predcup.pnl import PnlFeed, estimate_day_pnl
from predcup.risk import RiskLimits, RiskManager
from predcup.store import EventStore
from predcup.venues.sig import TournamentPnl
from sim.mock_exchange import MockExchange

TID = "550e8400-e29b-41d4-a716-446655440000"
T0 = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)


class Alerts:
    def __init__(self):
        self.messages = []

    def send(self, m):
        self.messages.append(m)


def fill(i, ex, side, qty, price):
    return Fill(id=str(i), order_id="", exchange_id=ex, tournament_id=TID, side=side, action="buy", quantity=qty,
                price=price, filled_at=T0)  # fmt: skip


# --- the estimate (pure) -------------------------------------------------------------


def test_estimate_marks_base_positions_and_new_fills_to_market():
    est = estimate_day_pnl(
        base_pnl=-10.0,
        base_positions={"e": 100},  # 100 YES
        base_marks={"e": 0.5},
        new_fills=[fill(1, "e", "yes", 50, 0.42)],  # bought 50 more YES at 0.42 after the base
        marks_now={"e": 0.4},
    )
    assert est == pytest.approx(-10.0 + 100 * (0.4 - 0.5) + 50 * (0.4 - 0.42))  # -21


def test_estimate_no_fill_is_a_yes_sale_at_the_yes_price():
    # Bought 1,000 NO at 0.92 = sold 1,000 YES at 0.08; YES now 0.02 -> +60.
    est = estimate_day_pnl(base_pnl=0.0, base_positions={}, base_marks={},
                           new_fills=[fill(1, "e", "no", 1000, 0.08)], marks_now={"e": 0.02})  # fmt: skip
    assert est == pytest.approx(60.0)


def test_estimate_netting_round_trip_is_its_spread():
    # Bought 10 YES at 0.40, then 10 NO at 0.45 YES-price (sold YES at 0.45): +0.5 whatever the mark.
    fills = [fill(1, "e", "yes", 10, 0.40), fill(2, "e", "no", 10, 0.45)]
    assert estimate_day_pnl(0.0, {}, {}, fills, {"e": 0.9}) == pytest.approx(0.5)


def test_unknown_marks_contribute_no_change():
    est = estimate_day_pnl(base_pnl=-5.0, base_positions={"e": 100}, base_marks={"e": None},
                           new_fills=[fill(1, "f", "yes", 10, 0.3)], marks_now={"e": 0.9, "f": None})  # fmt: skip
    assert est == pytest.approx(-5.0)


# --- the feed ------------------------------------------------------------------------


def make(tmp_path, *, sig, shadow=False, fills_fresh=True, marks=None, positions=None):
    clock = {"t": T0}
    store = EventStore(tmp_path / "e.db")
    alerts = Alerts()
    risk = RiskManager(
        limits=RiskLimits(max_bankroll_fraction_per_market=1, max_total_exposure_fraction=1,
                          max_party_exposure_fraction=1, max_order_size_susqies=1e9,
                          max_price_deviation_from_fair_value=1, daily_loss_stop_fraction=0.08,
                          stale_data_stop_seconds=60),  # fmt: skip
        bankroll=100_000.0, event_store=store, venue=MockExchange(), tournament_id=TID, alerter=alerts,
        size_ramp=full_size_ramp(), fusion_race_keys=frozenset(), now=lambda: clock["t"],
        require_daily_pnl=not shadow, daily_pnl_max_age_seconds=600,
    )  # fmt: skip

    class Venue(MockExchange):
        async def get_pnl(self, tournament_id, period):
            value = sig["value"]
            if value == "timeout":
                raise TimeoutError("portfolio read timed out")
            return TournamentPnl(period="day", period_pnl=value, unrealized_pnl=0.0,
                                 total_account_value=100_000.0 + (value or 0), roi=None)  # fmt: skip

    state = {"marks": marks or {}, "fresh": fills_fresh}

    async def mark_fn(exchanges):
        return {ex: state["marks"].get(ex) for ex in exchanges}

    feed = PnlFeed(venue=Venue(), store=store, risk=risk, alerter=alerts, tournament_id=TID, shadow=shadow,
                   marks=mark_fn, fills_fresh=lambda: state["fresh"], clock=lambda: clock["t"])  # fmt: skip
    return feed, risk, store, alerts, clock, state


def order():
    return Order(exchange_id="e", market_id="m", tournament_id=TID, party_id="D", race_key="R", side="yes",
                 action="buy", quantity=10, price=0.5, idempotency_key="k")  # fmt: skip


def check(risk):
    return risk.check(order(), fair_value=0.5, outside_data_age_seconds=0)


def test_sig_pnl_feeds_the_loss_stop(tmp_path):
    sig = {"value": -30.0}
    feed, risk, store, _, _, _ = make(tmp_path, sig=sig)
    assert asyncio.run(feed.run_once()) == "sig"
    assert risk._daily_realized_pnl == -30.0 and check(risk).approved
    assert store.all_events("pnl")[-1]["payload"]["source"] == "sig"


def test_slow_or_null_sig_pnl_falls_back_to_the_estimate(tmp_path):
    sig = {"value": -10.0}
    feed, risk, store, _, clock, state = make(tmp_path, sig=sig, marks={"1075": 0.5})
    store.record_fill(fill(1, "1075", "yes", 100, 0.5))  # position 100 YES before the base
    asyncio.run(feed.run_once())  # base: -10 with 100 YES marked 0.5
    store.record_fill(fill(2, "1075", "yes", 50, 0.42))
    state["marks"] = {"1075": 0.4}
    sig["value"] = "timeout"
    clock["t"] = T0 + timedelta(minutes=2)
    assert asyncio.run(feed.run_once()) == "estimate"
    assert risk._daily_realized_pnl == pytest.approx(-21.0)
    assert check(risk).approved
    sig["value"] = None  # null and not derivable: estimate again
    assert asyncio.run(feed.run_once()) == "estimate"


def test_loss_stop_triggers_on_the_estimate(tmp_path):
    sig = {"value": 0.0}
    feed, risk, store, _, _, state = make(tmp_path, sig=sig, marks={"e": 0.5})
    store.record_fill(fill(1, "e", "yes", 20_000, 0.5))
    asyncio.run(feed.run_once())
    state["marks"] = {"e": 0.05}  # -9,000 marked to market
    sig["value"] = "timeout"
    asyncio.run(feed.run_once())
    assert check(risk).reason == "daily loss stop triggered"


def test_blocked_only_when_both_are_unavailable(tmp_path):
    sig = {"value": "timeout"}
    feed, risk, store, alerts, _, _ = make(tmp_path, sig=sig)  # SIG never answered: no base for an estimate
    assert asyncio.run(feed.run_once()) == "unavailable"
    assert check(risk).reason.startswith("daily P&L unavailable")
    asyncio.run(feed.run_once())
    assert sum("Daily P&L unavailable" in m for m in alerts.messages) == 1
    sig["value"] = -5.0
    asyncio.run(feed.run_once())
    assert check(risk).approved and sum("available again" in m for m in alerts.messages) == 1


def test_estimate_needs_fresh_fills(tmp_path):
    sig = {"value": -10.0}
    feed, risk, _, _, _, state = make(tmp_path, sig=sig)
    asyncio.run(feed.run_once())
    sig["value"] = "timeout"
    state["fresh"] = False  # reconciliation hasn't synced fills recently: estimate unreliable
    assert asyncio.run(feed.run_once()) == "unavailable"
    assert not check(risk).approved


def test_ten_minutes_of_staleness_allowed(tmp_path):
    sig = {"value": -10.0}
    feed, risk, _, _, clock, _ = make(tmp_path, sig=sig)
    asyncio.run(feed.run_once())
    clock["t"] = T0 + timedelta(seconds=599)
    assert check(risk).approved
    clock["t"] = T0 + timedelta(seconds=601)
    assert "stale" in check(risk).reason


def test_shipped_staleness_is_ten_minutes():
    import yaml
    from pathlib import Path

    assert yaml.safe_load(Path("config/settings.yaml").read_text())["risk"]["daily_pnl_max_age_seconds"] == 600


# --- wiring ---------------------------------------------------------------------------


def test_pnl_runs_in_its_own_loop_and_reconciliation_never_reads_it(tmp_path):
    from test_app import BookVenue, make_app

    class Spy(BookVenue):
        def __init__(self):
            super().__init__()
            self.pnl_reads = 0

        async def get_pnl(self, tournament_id, period):
            self.pnl_reads += 1
            return await super().get_pnl(tournament_id, period)

    venue = Spy()
    app, store = make_app(tmp_path, venue=venue, clock=lambda: datetime.now(timezone.utc))
    asyncio.run(app.reconcile_once())
    assert venue.pnl_reads == 0
    asyncio.run(app.run(duration_seconds=0.3))
    assert venue.pnl_reads >= 1
    assert "pnl" in {e["payload"]["loop"] for e in store.all_events("loop_lag")}


def test_app_marks_prefer_kalshi_then_tight_sig_mid(tmp_path):
    from predcup.fairvalue import FairValue
    from test_app import BookVenue, make_app

    venue = BookVenue()
    venue.get_top_of_books = None  # replaced below
    now = datetime.now(timezone.utc)

    async def tops(ids, tid):
        return {"tight": (0.40, 0.42), "wide": (0.01, 0.97), "fv": (0.1, 0.9)}

    venue.get_top_of_books = tops
    app, _ = make_app(tmp_path, venue=venue, clock=lambda: now)
    app.fair_values._current["fv"] = FairValue(ok=True, value=0.61, uncertainty=0.01, as_of=now)
    marks = asyncio.run(app._pnl_marks(["fv", "tight", "wide", "none"]))
    assert marks == {"fv": 0.61, "tight": pytest.approx(0.41), "wide": None, "none": None}


def test_account_value_becomes_the_bankroll_even_when_pnl_is_null(tmp_path):
    sig = {"value": -30.0}
    feed, risk, _, _, _, _ = make(tmp_path, sig=sig)
    asyncio.run(feed.run_once())
    assert risk._bankroll == pytest.approx(99_970.0)  # totalAccountValue
    sig["value"] = None
    asyncio.run(feed.run_once())  # null P&L: bankroll still follows the account value
    assert risk._bankroll == pytest.approx(100_000.0)
