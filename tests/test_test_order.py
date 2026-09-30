"""Go-live gate (c)/(d): one tiny test order through the router and
risk.check_test_order(), confirmed, cancelled, confirmed gone; or left
resting for a /kill test. risk.check_test_order runs every normal check
except distance from fair value, plus hard limits (1 share, extreme
price), so a test order can never be a real trade."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from _helpers import full_size_ramp
from predcup.control import TradingControl
from predcup.models import Order
from predcup.orders import OrderRouter
from predcup.risk import RiskLimits, RiskManager
from predcup.store import EventStore
from scripts.place_test_order import RefusedError, check_trading_open, choose_test_quote, run_test_order, select_market
from sim.mock_exchange import MockExchange

TID = "550e8400-e29b-41d4-a716-446655440000"
OPEN = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)
CUP = [
    {"id": "379", "exchange_id": "1068", "title": "D MA Sen", "party": "D", "race_key": "MA-Senate"},
    {"id": "387", "exchange_id": "1076", "title": "D RI Sen", "party": "D", "race_key": "RI-Senate"},
]


class Alerts:
    def __init__(self):
        self.messages = []

    def send(self, m):
        self.messages.append(m)


def run(c):
    return asyncio.run(c)


def make(tmp_path, venue=None):
    store = EventStore(tmp_path / "t.db")
    venue = venue or MockExchange()
    risk = RiskManager(
        limits=RiskLimits(max_bankroll_fraction_per_market=0.05, max_total_exposure_fraction=0.6,
                          max_party_exposure_fraction=0.25, max_order_size_susqies=500,
                          max_price_deviation_from_fair_value=0.03, daily_loss_stop_fraction=0.08,
                          stale_data_stop_seconds=60),  # fmt: skip
        bankroll=100_000.0, event_store=store, venue=venue, tournament_id=TID, alerter=Alerts(),
        size_ramp=full_size_ramp(), fusion_race_keys=frozenset(),
    )  # fmt: skip
    router = OrderRouter(venue=venue, risk=risk, store=store, tournament_id=TID, shadow=False,
                         alerter=Alerts(), control=TradingControl())  # fmt: skip
    return router, risk, venue, store


def tiny_order(price=0.005, qty=1, action="buy", key="t1") -> Order:
    return Order(exchange_id="1068", market_id="379", tournament_id=TID, party_id="D", race_key="MA-Senate",
                 side="yes", action=action, quantity=qty, price=price, idempotency_key=key)  # fmt: skip


# --- risk.check_test_order -----------------------------------------------------------


def test_risk_allows_one_share_at_an_extreme_price(tmp_path):
    _, risk, _, store = make(tmp_path)
    assert risk.check_test_order(tiny_order()).approved
    assert risk.check_test_order(tiny_order(price=0.995, action="sell")).approved
    assert store.all_events("test_order_check")


@pytest.mark.parametrize("kw,reason", [
    ({"qty": 2}, "1 share"),
    ({"price": 0.5}, "extreme"),
    ({"price": 0.02}, "extreme"),
    ({"price": 0.98, "action": "sell"}, "extreme"),
    ({"price": 0.995, "action": "buy"}, "extreme"),  # a buy at 0.995 would trade
])  # fmt: skip
def test_risk_refuses_anything_that_could_be_a_real_trade(tmp_path, kw, reason):
    _, risk, _, _ = make(tmp_path)
    d = risk.check_test_order(tiny_order(**kw))
    assert not d.approved and reason in d.reason


def test_risk_test_order_respects_the_kill_latch(tmp_path):
    _, risk, _, _ = make(tmp_path)
    run(risk.kill("test"))
    d = risk.check_test_order(tiny_order())
    assert not d.approved and d.reason == "kill switch engaged"


def test_risk_test_order_respects_halted_market(tmp_path):
    _, risk, _, _ = make(tmp_path)
    risk.record_order_rejection("379", 400, "X", "y")
    assert not risk.check_test_order(tiny_order()).approved


# --- router.place_test_order -----------------------------------------------------------


def test_router_places_test_order_only_after_risk(tmp_path):
    router, risk, venue, store = make(tmp_path)
    placed = run(router.place_test_order(tiny_order()))
    assert placed is not None and placed.id
    assert [o.id for o in run(venue.get_open_orders(TID))] == [placed.id]
    assert store.all_events("test_order")[0]["payload"]["order_id"] == placed.id
    assert risk._tracked_orders()[0].idempotency_key == "t1"


def test_router_refuses_rejected_test_order(tmp_path):
    router, _, venue, _ = make(tmp_path)
    assert run(router.place_test_order(tiny_order(price=0.5))) is None
    assert run(venue.get_open_orders(TID)) == []


# --- script pieces --------------------------------------------------------------------------


def test_market_must_be_named_known_and_not_manual_only():
    assert select_market("379", CUP, manual_only=[])["exchange_id"] == "1068"
    with pytest.raises(RefusedError, match="manual_only"):
        select_market("379", CUP, manual_only=["MA-Senate"])
    with pytest.raises(RefusedError, match="manual_only"):
        select_market("387", CUP, manual_only=["387"])
    with pytest.raises(RefusedError, match="not a Cup market"):
        select_market("999", CUP, manual_only=[])


def test_refuses_before_trading_opens():
    summary = {"status": "draft", "startDate": "2026-10-01T16:00:00.000Z"}
    with pytest.raises(RefusedError, match="draft"):
        check_trading_open(summary, OPEN)
    summary = {"status": "active", "startDate": "2026-10-01T16:00:00.000Z"}
    with pytest.raises(RefusedError, match="opens"):
        check_trading_open(summary, datetime(2026, 10, 1, 15, 59, tzinfo=timezone.utc))
    check_trading_open(summary, OPEN)


def test_choose_quote_far_from_the_market():
    assert choose_test_quote(best_bid=0.40, best_ask=0.45) == ("buy", 0.005)
    assert choose_test_quote(best_bid=None, best_ask=None) == ("buy", 0.005)
    assert choose_test_quote(best_bid=0.001, best_ask=0.01) == ("sell", 0.995)  # a 0.005 bid would be too close
    with pytest.raises(RefusedError, match="far from the market"):
        choose_test_quote(best_bid=0.98, best_ask=0.02)  # crossed: nowhere is 2 points clear


def test_full_run_places_confirms_cancels_and_confirms_gone(tmp_path):
    router, _, venue, store = make(tmp_path)
    venue.set_top_of_book("1068", 0.94, 0.96)
    lines = run(run_test_order(router=router, venue=venue, tournament_id=TID, market=CUP[0],
                               leave_resting=False, expiry_seconds=120, now=OPEN))  # fmt: skip
    text = "\n".join(lines)
    assert "seen in open orders" in text and "gone from open orders" in text
    assert run(venue.get_open_orders(TID)) == []


def test_leave_resting_keeps_the_order_for_a_kill_test(tmp_path):
    router, _, venue, _ = make(tmp_path)
    lines = run(run_test_order(router=router, venue=venue, tournament_id=TID, market=CUP[0],
                               leave_resting=True, expiry_seconds=600, now=OPEN))  # fmt: skip
    [o] = run(venue.get_open_orders(TID))
    assert o.price == 0.005 and o.quantity == 1
    assert any("left resting" in line for line in lines)


def test_run_stops_if_risk_refuses(tmp_path):
    router, risk, venue, _ = make(tmp_path)
    run(risk.kill("already killed"))
    with pytest.raises(RefusedError, match="refused"):
        run(run_test_order(router=router, venue=venue, tournament_id=TID, market=CUP[0],
                           leave_resting=False, expiry_seconds=120, now=OPEN))  # fmt: skip


def test_script_requires_market_argument():
    from scripts.place_test_order import parse_args

    with pytest.raises(SystemExit):
        parse_args([])
    assert parse_args(["--market", "379"]).market == "379"
