"""Daily loss stop must fail closed in live mode (2026-10-01): with no
daily P&L, or P&L older than risk.daily_pnl_max_age_seconds, every new
order is refused. Before, a missing P&L left the stop checking a stale
value (initially 0): trading with no loss stop."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from _helpers import full_size_ramp
from predcup.models import Order
from predcup.risk import RiskLimits, RiskManager
from predcup.store import EventStore
from sim.mock_exchange import MockExchange

TID = "550e8400-e29b-41d4-a716-446655440000"
T0 = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)


class Alerts:
    def __init__(self):
        self.messages = []

    def send(self, m):
        self.messages.append(m)


def make(tmp_path, clock, require=True):
    return RiskManager(
        limits=RiskLimits(max_bankroll_fraction_per_market=1, max_total_exposure_fraction=1,
                          max_party_exposure_fraction=1, max_order_size_susqies=1e9,
                          max_price_deviation_from_fair_value=1, daily_loss_stop_fraction=0.08,
                          stale_data_stop_seconds=60),  # fmt: skip
        bankroll=100_000.0, event_store=EventStore(tmp_path / "e.db"), venue=MockExchange(), tournament_id=TID,
        alerter=Alerts(), size_ramp=full_size_ramp(), fusion_race_keys=frozenset(), now=lambda: clock["t"],
        require_daily_pnl=require, daily_pnl_max_age_seconds=180,
    )  # fmt: skip


def order():
    return Order(exchange_id="e", market_id="m", tournament_id=TID, party_id="D", race_key="R", side="yes",
                 action="buy", quantity=10, price=0.5, idempotency_key="k")  # fmt: skip


def check(risk):
    return risk.check(order(), fair_value=0.5, outside_data_age_seconds=0)


def test_live_refuses_orders_until_daily_pnl_is_known(tmp_path):
    clock = {"t": T0}
    risk = make(tmp_path, clock)
    d = check(risk)
    assert not d.approved and d.reason.startswith("daily P&L unavailable")
    risk.update_daily_pnl(-30.0)
    assert check(risk).approved


def test_live_refuses_orders_when_daily_pnl_goes_stale(tmp_path):
    clock = {"t": T0}
    risk = make(tmp_path, clock)
    risk.update_daily_pnl(-30.0)
    clock["t"] = T0 + timedelta(seconds=181)
    d = check(risk)
    assert not d.approved and "stale" in d.reason


def test_live_refuses_orders_when_pnl_becomes_unavailable(tmp_path):
    clock = {"t": T0}
    risk = make(tmp_path, clock)
    risk.update_daily_pnl(-30.0)
    risk.update_daily_pnl(None)  # read failed / null and not derivable
    assert not check(risk).approved


def test_loss_stop_still_triggers_on_a_known_loss(tmp_path):
    clock = {"t": T0}
    risk = make(tmp_path, clock)
    risk.update_daily_pnl(-9_000.0)
    assert check(risk).reason == "daily loss stop triggered"


def test_shadow_does_not_block_on_missing_pnl(tmp_path):
    clock = {"t": T0}
    assert check(make(tmp_path, clock, require=False)).approved


def test_reconciler_alerts_when_pnl_becomes_unavailable_and_again_on_recovery(tmp_path):
    from predcup.control import TradingControl
    from predcup.reconcile import Reconciler
    from predcup.venues.sig import TournamentPnl

    clock = {"t": T0}
    risk = make(tmp_path, clock)
    alerts = Alerts()
    state = {"pnl": None}

    class Venue(MockExchange):
        async def get_pnl(self, tournament_id, period):
            if state["pnl"] == "fail":
                raise TimeoutError("SIG slow")
            return TournamentPnl(period="day", period_pnl=state["pnl"], unrealized_pnl=0.0,
                                 total_account_value=100_000.0, roi=None)  # fmt: skip

    class Router:
        def swept_snapshot(self):
            return []

        def release_swept(self, keys):
            pass

        def clear_block(self, reason):
            pass

    store = EventStore(tmp_path / "r.db")
    rec = Reconciler(venue=Venue(), store=store, risk=risk, router=Router(), control=TradingControl(),
                     alerter=alerts, tournament_id=TID, market_meta={}, shadow=False, clock=lambda: clock["t"])  # fmt: skip
    asyncio.run(rec.run_once())  # null, not derivable
    asyncio.run(rec.run_once())  # still null: no second alert
    state["pnl"] = "fail"
    asyncio.run(rec.run_once())  # read failure: still unavailable, still one alert
    assert not check(risk).approved
    assert sum("Daily P&L unavailable" in m for m in alerts.messages) == 1
    state["pnl"] = -12.0
    asyncio.run(rec.run_once())
    assert check(risk).approved
    assert sum("Daily P&L available again" in m for m in alerts.messages) == 1
    assert store.all_events("pnl_unavailable")


def test_app_requires_daily_pnl_in_live_mode_only(tmp_path):
    from test_app import make_app

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    live, _ = make_app(tmp_path / "a", shadow=False, live_allowed=True)
    shadow, _ = make_app(tmp_path / "b")
    assert live.risk.require_daily_pnl is True and shadow.risk.require_daily_pnl is False
