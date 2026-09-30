"""Positions in risk exposure (2026-09-30). Exposure used to come from
orders only, so a quote that filled and was then swept by cancel-all
dropped out of every cap. The reconciliation loop now feeds venue
positions in; once it has, fully-filled orders stop counting (their
exposure lives in the position) so nothing is double-counted."""

from __future__ import annotations

import pytest

from _helpers import full_size_ramp
from predcup.models import Order, OrderStatus
from predcup.risk import PositionExposure, RiskLimits, RiskManager
from predcup.store import EventStore
from sim.mock_exchange import MockExchange

TID = "550e8400-e29b-41d4-a716-446655440000"


class Alerts:
    def send(self, m):
        pass


@pytest.fixture
def risk(tmp_path):
    return RiskManager(
        limits=RiskLimits(
            max_bankroll_fraction_per_market=0.05,  # 10,000 -> 500 per market
            max_total_exposure_fraction=0.10,  # 1,000 total
            max_party_exposure_fraction=0.06,  # 600 net R-vs-D
            max_order_size_susqies=1_000_000, max_price_deviation_from_fair_value=1.0,
            daily_loss_stop_fraction=1.0, stale_data_stop_seconds=60,
        ),  # fmt: skip
        bankroll=10_000.0, event_store=EventStore(tmp_path / "e.db"), venue=MockExchange(), tournament_id=TID,
        alerter=Alerts(), size_ramp=full_size_ramp(), fusion_race_keys=frozenset({"NY-Senate"}),
    )  # fmt: skip


def order(key="o", market_id="m1", race_key="MI-Senate", party="R", qty=200, price=0.5, action="buy"):
    return Order(exchange_id="e-" + market_id, market_id=market_id, tournament_id=TID, party_id=party,
                 race_key=race_key, side="yes", action=action, quantity=qty, price=price, idempotency_key=key)  # fmt: skip


def check(risk, o):
    return risk.check(o, fair_value=o.price, outside_data_age_seconds=0)


def pos(market_id="m1", qty=800, price=0.5, party="R", race_key="MI-Senate"):
    return PositionExposure(market_id=market_id, party_id=party, race_key=race_key, quantity=qty, price=price)


def test_position_counts_toward_per_market_cap(risk):
    risk.update_positions([pos(qty=800, price=0.5)])  # 400 in m1
    assert check(risk, order(qty=180)).approved  # +90 = 490
    d = check(risk, order(qty=220))  # +110 = 510 > 500
    assert not d.approved and "per-market" in d.reason


def test_no_shares_use_the_no_price(risk):
    risk.update_positions([pos(qty=-800, price=0.9)])  # 800 NO shares at 0.10 = 80
    assert check(risk, order(qty=800, price=0.5)).approved  # 80 + 400 = 480


def test_positions_count_toward_total_cap(risk):
    risk.update_positions([pos("m1", 800, 0.5), pos("m2", 800, 0.5, race_key="OH-Senate")])  # 800 total
    d = check(risk, order(market_id="m3", race_key="TX-Senate", party="D", qty=500, price=0.5))  # +250
    assert not d.approved and "total" in d.reason


def test_positions_count_on_the_party_axis(risk):
    risk.update_positions([pos(qty=800, price=0.5, party="R")])  # long R 400
    d = check(risk, order(market_id="m2", race_key="OH-Senate", party="R", qty=500, price=0.5))  # +250 -> 650
    assert not d.approved and "party-exposure" in d.reason


def test_fusion_race_positions_stay_off_the_party_axis(risk):
    risk.update_positions([
        pos("m3", 800, 0.5, party="R", race_key="OH-Senate"),  # long R 400
        pos("m4", 800, 0.5, party="D", race_key="NY-Senate"),  # fusion race: must not offset
    ])  # fmt: skip
    # Net R would be 400 - 400 + 250 = 250 if the fusion D position offset it; it must be 650 > 600.
    d = check(risk, order(market_id="m2", race_key="MI-Senate", party="R", qty=500, price=0.5))
    assert not d.approved and "party-exposure" in d.reason


def test_filled_orders_count_until_positions_are_known_then_positions_take_over(risk):
    risk.record_order(order("f", qty=800, price=0.5))
    risk.confirm_order_state("f", OrderStatus.FILLED)
    d = check(risk, order("n", qty=300, price=0.5))  # 400 filled + 150 > 500
    assert not d.approved
    risk.update_positions([pos(qty=800, price=0.5)])  # the fill is now the position (400)
    d = check(risk, order("n", qty=300, price=0.5))  # still 400 + 150: not double-counted as 950
    assert not d.approved and "per-market" in d.reason
    assert check(risk, order("n2", qty=180, price=0.5)).approved  # 400 + 90


def test_update_positions_replaces_previous_snapshot(risk):
    risk.update_positions([pos(qty=900, price=0.5)])
    risk.update_positions([])
    assert check(risk, order(qty=900, price=0.5)).approved
