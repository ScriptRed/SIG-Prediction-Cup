"""Audit 2026-10-01 H3: an order's exposure is what it can lose, i.e. the
capital it ties up. Prices are YES-normalized: buying YES at p costs p a
share; selling YES at p (= buying NO at 1 - p) costs 1 - p a share.
Previously every order counted quantity x p, so selling a 0.05 longshot
counted 0.05 a share instead of 0.95."""

from __future__ import annotations

import pytest

from _helpers import full_size_ramp
from predcup.models import Order
from predcup.risk import RiskLimits, RiskManager, _order_notional
from predcup.store import EventStore
from sim.mock_exchange import MockExchange

TID = "550e8400-e29b-41d4-a716-446655440000"


def o(side="yes", action="buy", price=0.05, qty=100, key="k", market_id="m1"):
    return Order(exchange_id="e1", market_id=market_id, tournament_id=TID, party_id="R", race_key="MI-Senate",
                 side=side, action=action, quantity=qty, price=price, idempotency_key=key)  # fmt: skip


@pytest.mark.parametrize("side,action,price,expected", [
    ("yes", "buy", 0.05, 5.0),     # long YES at 0.05
    ("yes", "sell", 0.05, 95.0),   # short YES = long NO at 0.95
    ("yes", "sell", 0.95, 5.0),
    ("no", "buy", 0.05, 95.0),     # NO at YES price 0.05 costs 0.95
    ("no", "sell", 0.05, 5.0),     # = buy YES at 0.05
])  # fmt: skip
def test_notional_is_the_capital_at_risk(side, action, price, expected):
    assert _order_notional(o(side=side, action=action, price=price)) == pytest.approx(expected)


def test_market_order_counts_full_quantity():
    assert _order_notional(Order(exchange_id="e1", tournament_id=TID, side="yes", action="buy", quantity=10,
                                 idempotency_key="m")) == 10  # fmt: skip


class Alerts:
    def send(self, m):
        pass


def test_selling_a_cheap_longshot_hits_the_per_market_cap(tmp_path):
    risk = RiskManager(
        limits=RiskLimits(max_bankroll_fraction_per_market=0.05, max_total_exposure_fraction=1.0,
                          max_party_exposure_fraction=1.0, max_order_size_susqies=1e9,
                          max_price_deviation_from_fair_value=1.0, daily_loss_stop_fraction=1.0,
                          stale_data_stop_seconds=60),  # fmt: skip
        bankroll=1000.0, event_store=EventStore(tmp_path / "e.db"), venue=MockExchange(), tournament_id=TID,
        alerter=Alerts(), size_ramp=full_size_ramp(), fusion_race_keys=frozenset(),
    )  # fmt: skip
    # Per-market cap 50. Buying 100 YES at 0.05 risks 5; selling them risks 95.
    assert risk.check(o(action="buy"), fair_value=0.05, outside_data_age_seconds=0).approved
    d = risk.check(o(action="sell", key="s"), fair_value=0.05, outside_data_age_seconds=0)
    assert not d.approved and "per-market" in d.reason
