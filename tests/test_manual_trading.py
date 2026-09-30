"""Manual trading on the same account coexists with the bot (2026-09-30):
- markets listed in settings trading.manual_only are never quoted;
- reconciliation ingests every fill on the account, so a trade placed by
  hand never causes a mismatch;
- manual positions count toward the bot's risk limits."""

from __future__ import annotations

import asyncio

import pytest

from predcup.app import load_targets
from predcup.models import Order
from test_app import CUP, MAP, TID, BookVenue, make_app, mrow


def run(c):
    return asyncio.run(c)


ALL_VERIFIED = [mrow("379", "SENATEMA-26-D"), mrow("380", "SENATEMA-26-R"), mrow("370", "GOVPARTYMA-26-D")]


def test_manual_only_by_race_key():
    targets = load_targets(CUP, ALL_VERIFIED, manual_only=["MA-Senate"])
    assert [t.target.market_id for t in targets] == ["370"]


def test_manual_only_by_market_id():
    targets = load_targets(CUP, ALL_VERIFIED, manual_only=["380"])
    assert [t.target.market_id for t in targets] == ["379", "370"]


def test_unknown_manual_only_entry_is_an_error():
    # A typo must not silently leave a market open to the bot.
    with pytest.raises(ValueError, match="MA-Senat"):
        load_targets(CUP, ALL_VERIFIED, manual_only=["MA-Senat"])


def test_manual_only_is_required_explicitly():
    with pytest.raises(TypeError):
        load_targets(CUP, ALL_VERIFIED)  # type: ignore[call-arg]


def manual_order(key, exchange_id="1059", market_id="370", qty=100, price=0.5, action="buy"):
    return Order(exchange_id=exchange_id, market_id=market_id, tournament_id=TID, side="yes", action=action,
                 quantity=qty, price=price, idempotency_key=key)  # fmt: skip


def test_manual_fill_reconciles_clean_and_counts_in_risk(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    placed = run(venue.place_order(manual_order("by-hand")))  # web UI, not the bot
    run(venue.simulate_fill(placed.id, 100))
    run(app.reconcile_once())
    assert store.all_events("reconciliation")[-1]["payload"]["status"] == "clean"
    [p] = app.risk._positions
    assert (p.market_id, p.party_id, p.race_key, p.quantity) == ("370", "D", "MA-Governor", 100)


def test_manual_round_trip_nets_to_flat_and_reconciles(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    buy = run(venue.place_order(manual_order("b")))
    run(venue.simulate_fill(buy.id, 100))
    sell = run(venue.place_order(manual_order("s", action="sell", price=0.55)))
    run(venue.simulate_fill(sell.id, 100))
    run(app.reconcile_once())
    assert store.all_events("reconciliation")[-1]["payload"]["status"] == "clean"
    assert store.local_positions(TID) == {"1059": 0}


def test_manual_position_limits_what_the_bot_may_add(tmp_path):
    venue = BookVenue()
    app, _ = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    # A large manual D position elsewhere: 20,000 x 0.5 = 10,000 notional.
    placed = run(venue.place_order(manual_order("big", qty=20_000)))
    run(venue.simulate_fill(placed.id, 20_000))
    run(app.reconcile_once())
    # max_party_exposure_fraction 0.25 of 100,000 = 25,000; total cap 60,000.
    bot_order = Order(exchange_id="1068", market_id="379", tournament_id=TID, party_id="D", race_key="MA-Senate",
                      side="yes", action="buy", quantity=31_000, price=0.5, idempotency_key="bot")  # fmt: skip
    app.risk._limits = app.risk._limits.__class__(**{**app.risk._limits.__dict__,
                                                     "max_order_size_susqies": 1e9,
                                                     "max_bankroll_fraction_per_market": 1.0})  # fmt: skip
    app.ramp.step = app.ramp._max_step  # full size, only the party cap under test
    d = app.risk.check(bot_order, fair_value=0.5, outside_data_age_seconds=0)
    assert not d.approved and "party-exposure" in d.reason  # 10,000 manual + 15,500 bot > 25,000
