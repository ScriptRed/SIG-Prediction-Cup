"""Reconciliation loop (CLAUDE.md): every minute sync fills, compare local
positions (from fills) with /tournaments/{slug}/portfolio/positions; on
mismatch cancel all, halt, alert. Clean -> ramp credit, positions into
risk, swept quotes released, router unblocked. Shadow mode compares and
alerts but never moves the ramp or halts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from predcup.control import TradingControl
from predcup.models import Fill, Position
from predcup.reconcile import Reconciler
from predcup.store import EventStore
from predcup.venues.sig import SigApiError

TID = "550e8400-e29b-41d4-a716-446655440000"
T0 = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)


def fill(i, qty, ex="1077"):
    return Fill(id=str(i), order_id=str(100 + i), exchange_id=ex, tournament_id=TID, side="yes" if qty > 0 else "no",
                action="buy", quantity=abs(qty), price=0.5, filled_at=T0)  # fmt: skip


class FakeVenue:
    def __init__(self):
        self.fills: list[Fill] = []
        self.positions: list[Position] = []
        self.fail_positions = False
        self.fill_calls: list[set] = []

    async def get_new_fills(self, tournament_id, known_ids):
        self.fill_calls.append(set(known_ids))
        return [f for f in self.fills if f.id not in known_ids]

    async def get_positions(self, tournament_id):
        if self.fail_positions:
            raise SigApiError(503, "SERVICE_UNAVAILABLE", "down")
        return self.positions


class FakeRisk:
    def __init__(self):
        self.recon: list[tuple[bool, str]] = []
        self.positions = None

    def record_reconciliation(self, matched, detail=""):
        self.recon.append((matched, detail))

    def update_positions(self, positions, *, as_of):
        self.positions = positions
        self.positions_as_of = as_of

    def schedule_markouts(self, fill, lookup):
        return []

    def update_bankroll(self, bankroll):
        self.bankroll = bankroll

    def update_daily_pnl(self, pnl):
        self.daily_pnl = pnl


class FakeRouter:
    def __init__(self):
        self.snapshots = 0
        self.released: list[list[str]] = []
        self.cleared = 0

    def swept_snapshot(self):
        self.snapshots += 1
        return ["k1"]

    def release_swept(self, keys):
        self.released.append(keys)

    def clear_block(self, reason):
        self.cleared += 1


class Alerts:
    def __init__(self):
        self.messages = []

    def send(self, m):
        self.messages.append(m)


META = {"1077": ("388", "R", "RI-Senate")}


def make(tmp_path, shadow=False, max_failures=3):
    venue, risk, router, control, alerts = FakeVenue(), FakeRisk(), FakeRouter(), TradingControl(), Alerts()
    store = EventStore(tmp_path / "e.db")
    rec = Reconciler(venue=venue, store=store, risk=risk, router=router, control=control, alerter=alerts,
                     tournament_id=TID, market_meta=META, shadow=shadow, max_read_failures=max_failures)  # fmt: skip
    return rec, venue, store, risk, router, control, alerts


def run(c):
    return asyncio.run(c)


def position(qty, ex="1077", price=0.4):
    return Position(exchange_id=ex, market_id="388", tournament_id=TID, quantity=qty, avg_cost=0.5, current_price=price)


def test_clean_when_fills_explain_positions(tmp_path):
    rec, venue, store, risk, router, control, _ = make(tmp_path)
    venue.fills = [fill(1, 10), fill(2, -3)]
    venue.positions = [position(7)]
    assert run(rec.run_once()).status == "clean"
    assert store.local_positions(TID) == {"1077": 7}
    assert risk.recon == [(True, "")]
    assert [(p.market_id, p.party_id, p.race_key, p.quantity, p.price) for p in risk.positions] == [("388", "R", "RI-Senate", 7, 0.4)]
    assert router.released == [["k1"]] and router.cleared == 1
    assert not control.halted


def test_fills_are_synced_incrementally(tmp_path):
    rec, venue, store, *_ = make(tmp_path)
    venue.fills = [fill(1, 10)]
    venue.positions = [position(10)]
    run(rec.run_once())
    venue.fills.append(fill(2, 5))
    venue.positions = [position(15)]
    run(rec.run_once())
    assert venue.fill_calls[1] == {"1"}
    assert store.local_positions(TID) == {"1077": 15}


def test_mismatch_halts_alerts_and_drops_the_ramp(tmp_path):
    rec, venue, store, risk, router, control, alerts = make(tmp_path)
    venue.fills = [fill(1, 10)]
    venue.positions = [position(12)]
    res = run(rec.run_once())
    assert res.status == "mismatch" and "1077" in res.detail
    assert risk.recon[0][0] is False
    assert control.halted and "reconciliation mismatch" in control.reason
    assert router.released == [] and router.cleared == 0
    assert any("mismatch" in m for m in alerts.messages)
    assert store.all_events("reconciliation")[-1]["payload"]["status"] == "mismatch"


def test_position_missing_locally_or_at_venue_is_a_mismatch(tmp_path):
    rec, venue, *_ = make(tmp_path)
    venue.positions = [position(5, ex="999")]
    assert run(rec.run_once()).status == "mismatch"


def test_zero_rows_are_ignored(tmp_path):
    rec, venue, *_ = make(tmp_path)
    venue.fills = [fill(1, 5), fill(2, -5)]
    venue.positions = [position(0)]
    assert run(rec.run_once()).status == "clean"


def test_read_failures_alert_then_halt_after_limit(tmp_path):
    rec, venue, store, risk, router, control, alerts = make(tmp_path, max_failures=3)
    venue.fail_positions = True
    for _ in range(2):
        assert run(rec.run_once()).status == "read_failed"
    assert not control.halted and risk.recon == []
    run(rec.run_once())
    assert control.halted and "reconciliation" in control.reason
    venue.fail_positions = False
    assert run(rec.run_once()).status == "clean"  # counter resets on success


def test_shadow_compares_but_never_moves_ramp_or_halts(tmp_path):
    rec, venue, store, risk, router, control, alerts = make(tmp_path, shadow=True)
    venue.positions = [position(3)]  # e.g. a manual trade we haven't seen fills for
    assert run(rec.run_once()).status == "mismatch"
    assert risk.recon == [] and not control.halted
    assert any("mismatch" in m for m in alerts.messages)
    venue.positions = []
    assert run(rec.run_once()).status == "clean"
    assert risk.recon == [] and risk.positions == []


# --- audit 2026-10-01 M2: a fill landing between the fill sync and the positions read ------


class RacyVenue(FakeVenue):
    """The positions read already includes a fill the fills read didn't have
    yet (it landed in between); the next fills read has it."""

    def __init__(self):
        super().__init__()
        self.pending_fill = fill(2, 5)

    async def get_positions(self, tournament_id):
        if self.pending_fill is not None:
            self.fills.append(self.pending_fill)
            self.pending_fill = None
        return await super().get_positions(tournament_id)


def test_fill_landing_mid_reconciliation_is_not_a_mismatch(tmp_path):
    rec, venue, store, risk, router, control, alerts = make(tmp_path)
    racy = RacyVenue()
    racy.fills = [fill(1, 10)]
    racy.positions = [position(15)]
    rec._venue = racy
    res = run(rec.run_once())
    assert res.status == "clean"
    assert not control.halted and risk.recon == [(True, "")]
    assert store.local_positions(TID) == {"1077": 15}


def test_persistent_mismatch_still_halts_after_the_recheck(tmp_path):
    rec, venue, store, risk, router, control, alerts = make(tmp_path)
    venue.fills = [fill(1, 10)]
    venue.positions = [position(12)]
    assert run(rec.run_once()).status == "mismatch"
    assert len(venue.fill_calls) == 2  # synced twice before declaring it
    assert control.halted
