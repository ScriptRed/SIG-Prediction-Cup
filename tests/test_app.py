"""predcup/app.py: wiring for the trading process in shadow mode. Live mode
is refused until the user says go (PLAN launch status)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from predcup.app import App, LIVE_ENABLED, load_targets
from predcup.models import Order
from predcup.store import EventStore
from predcup.venues.kalshi import parse_market
from sim.mock_exchange import MockExchange

TID = "550e8400-e29b-41d4-a716-446655440000"
NOW = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)

SETTINGS = {
    "risk": {
        "max_bankroll_fraction_per_market": 0.05, "max_total_exposure_fraction": 0.6,
        "max_party_exposure_fraction": 0.25, "max_order_size_susqies": 500,
        "max_price_deviation_from_fair_value": 0.03, "daily_loss_stop_fraction": 0.08,
        "stale_data_stop_seconds": 60, "reconciliation_interval_seconds": 60, "reconciliation_max_read_failures": 3,
        "size_ramp": {"launch_fraction": 0.1, "step_multiplier": 2.0, "clean_reconciliations_per_step": 45,
                      "rate_limit_max_in_window": 5, "rate_limit_window_seconds": 600},
    },
    "loop_lag": {"alert_threshold_seconds": 1.0, "alert_cooldown_seconds": 60, "probe_interval_seconds": 0.05},
    "fair_value": {"max_outside_data_age_seconds": 60, "min_confidence_to_trade": 0.5,
                   "kalshi": {"max_spread": 0.1, "base_uncertainty": 0.005, "thin_volume": 1000,
                              "thin_penalty": 0.01, "stale_penalty": 0.01}},
    "quoter": {"min_edge": 0.01, "uncertainty_coefficient": 1.0, "inventory_skew_coefficient": 0.02,
               "requote_fair_value_move_ticks": 2, "quote_expiration_seconds": 30, "refresh_interval_seconds": 20,
               "cycle_interval_seconds": 0.05, "quote_size_shares": 20, "max_markets": 30,
               "max_position_shares": 200, "post_only": True, "pull_quotes_before_events": []},
    "venues": {"kalshi": {"poll_interval_seconds": 0.05}},
}  # fmt: skip

CUP = [
    {"id": "379", "exchange_id": "1068", "title": "D MA Sen", "state": "MA", "office": "Senate", "district": "",
     "party": "D", "race_key": "MA-Senate"},
    {"id": "380", "exchange_id": "1069", "title": "R MA Sen", "state": "MA", "office": "Senate", "district": "",
     "party": "R", "race_key": "MA-Senate"},
    {"id": "370", "exchange_id": "1059", "title": "D MA Gov", "state": "MA", "office": "Governor", "district": "",
     "party": "D", "race_key": "MA-Governor"},
]  # fmt: skip


def mrow(pid, ticker, verified="true", tier="A"):
    return {"platform_id": pid, "kalshi_ticker": ticker, "poly_token_id": "", "polarity": "same",
            "rule_diff_notes": "", "confidence": "0.8", "verified": verified, "tier": tier, "fusion_risk": "false"}  # fmt: skip


MAP = [mrow("379", "SENATEMA-26-D"), mrow("380", "SENATEMA-26-R", verified="false"), mrow("370", "GOVPARTYMA-26-D", tier="")]


def km(ticker, bid, ask):
    return parse_market({"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "title": "", "subtitle": "",
                         "yes_sub_title": "", "no_sub_title": "", "status": "active", "yes_bid_dollars": bid,
                         "yes_ask_dollars": ask, "volume_fp": "50000.00", "rules_primary": "", "rules_secondary": ""})  # fmt: skip


class FakeKalshi:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.fail = False

    async def get_markets(self, tickers):
        self.calls.append(list(tickers))
        if self.fail:
            raise RuntimeError("kalshi down")
        return {"SENATEMA-26-D": km("SENATEMA-26-D", "0.6000", "0.6200")}


class BookVenue(MockExchange):
    async def get_top_of_books(self, exchange_ids, tournament_id):
        return {e: (None, None) for e in exchange_ids}


class Alerts:
    def __init__(self):
        self.messages = []

    def send(self, m):
        self.messages.append(m)


def make_app(tmp_path, *, shadow=True, live_allowed=False, venue=None, kalshi=None, clock=None):
    store = EventStore(tmp_path / "e.db")
    return App(
        settings=SETTINGS, venue=venue or BookVenue(), kalshi=kalshi or FakeKalshi(), store=store,
        alerter=Alerts(), tournament_id=TID, bankroll=100_000.0,
        targets=load_targets(CUP, MAP), market_meta={c["exchange_id"]: (c["id"], c["party"], c["race_key"]) for c in CUP},
        fusion_race_keys=frozenset(), shadow=shadow,
        live_allowed=live_allowed, clock=clock or (lambda: NOW),
    ), store  # fmt: skip


def run(c):
    return asyncio.run(c)


def test_live_mode_is_disabled_in_this_build():
    assert LIVE_ENABLED is False


def test_live_mode_refused_without_explicit_permission(tmp_path):
    with pytest.raises(RuntimeError, match="live"):
        make_app(tmp_path, shadow=False, live_allowed=False)


def test_load_targets_only_verified_tier_a_with_ticker():
    targets = load_targets(CUP, MAP)
    assert [(t.target.exchange_id, t.map_row["kalshi_ticker"]) for t in targets] == [("1068", "SENATEMA-26-D")]
    assert targets[0].target.race_key == "MA-Senate" and targets[0].target.party == "D"


def test_one_step_in_shadow_logs_quotes_and_places_nothing(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, venue=venue)
    run(app.poll_kalshi_once())
    run(app.quote_once())
    assert run(venue.get_open_orders(TID)) == []
    quotes = store.all_events("shadow_quote")
    assert [(q["payload"]["action"], q["payload"]["price"]) for q in quotes] == [("buy", 0.595), ("sell", 0.625)]  # fv 0.61, half-spread 0.01 + base 0.005
    assert store.all_events("fair_value")[0]["payload"]["value"] == pytest.approx(0.61)


def test_kalshi_outage_makes_fair_value_go_stale_and_quotes_stop(tmp_path):
    t = {"now": NOW}
    kalshi = FakeKalshi()
    app, store = make_app(tmp_path, kalshi=kalshi, clock=lambda: t["now"])
    run(app.poll_kalshi_once())
    run(app.quote_once())
    kalshi.fail = True
    t["now"] = NOW + timedelta(seconds=61)
    run(app.poll_kalshi_once())  # fails, logged, keeps old quote (now stale)
    run(app.quote_once())
    assert store.all_events("kalshi_poll_failed")
    last_fv = store.all_events("fair_value")[-1]["payload"]
    assert last_fv["value"] is None and last_fv["reason"].startswith("stale")
    assert [e["payload"]["exchange_id"] for e in store.all_events("shadow_cancel")] == ["1068", "1068"]  # post, then pull


def test_request_kill_in_shadow_halts_and_logs(tmp_path):
    app, store = make_app(tmp_path)
    app.request_kill("KILL file")
    run(app.handle_halt_once())
    assert app.control.halted
    assert store.all_events("kill_switch_shadow")[0]["payload"]["reason"] == "KILL file"
    run(app.quote_once())
    assert store.all_events("shadow_quote") == []


def test_request_kill_live_runs_kill_switch(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    run(venue.place_order(Order(exchange_id="1068", tournament_id=TID, side="yes", action="buy", quantity=1,
                                price=0.5, idempotency_key="x")))  # fmt: skip
    app.request_kill("Telegram /kill")
    run(app.handle_halt_once())
    assert run(venue.get_open_orders(TID)) == []
    assert store.all_events("kill_switch")


def test_reset_ramp_hook(tmp_path):
    app, store = make_app(tmp_path)
    app.reset_ramp("Telegram /resetramp")
    assert any(e["payload"].get("action") == "reset" for e in store.all_events("size_ramp"))


def test_run_for_a_short_while_records_loop_lag(tmp_path):
    app, store = make_app(tmp_path, clock=lambda: datetime.now(timezone.utc))
    run(app.run(duration_seconds=0.3))
    loops = {e["payload"]["loop"] for e in store.all_events("loop_lag")}
    assert {"quoter", "kalshi_poll", "reconciliation", "event_loop"} <= loops
    assert store.all_events("reconciliation")[0]["payload"]["status"] == "clean"
    assert store.all_events("shadow_quote")


def test_live_fill_flows_through_reconciliation_into_risk(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    run(app.poll_kalshi_once())
    run(app.quote_once())
    [bid, ask] = sorted(run(venue.get_open_orders(TID)), key=lambda o: o.action)
    run(venue.simulate_fill(bid.id, 20))
    run(app.reconcile_once())
    assert store.local_positions(TID) == {"1068": 20}
    assert store.all_events("reconciliation")[-1]["payload"]["status"] == "clean"
    assert app.risk._positions[0].quantity == 20 and app.risk._positions[0].race_key == "MA-Senate"
