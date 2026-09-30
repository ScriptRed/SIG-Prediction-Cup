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
