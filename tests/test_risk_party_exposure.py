"""Per-market caps don't cover correlated polling error: several different
race markets can all be a bet on the same party, and a bad polling miss
moves them together. This is the net party-exposure limit — total
directional exposure to one major party across all races.

Independent markets are handled explicitly (not folded into the R-vs-D
axis, and not subject to this cap at all): see
test_independent_orders_are_excluded_from_the_rd_axis and
test_independent_orders_never_trigger_the_party_cap below, and
predcup/risk.py's net_rd_exposure docstring for the reasoning.
"""

import pytest

from _helpers import full_size_ramp
from predcup.models import Order
from predcup.risk import RiskLimits, RiskManager, net_rd_exposure
from predcup.store import EventStore
from sim.mock_exchange import MockExchange

TOURNAMENT_ID = "550e8400-e29b-41d4-a716-446655440000"


class FakeAlerter:
    def __init__(self):
        self.messages = []

    def send(self, message: str) -> None:
        self.messages.append(message)


def make_order(**overrides):
    fields = dict(
        exchange_id="36",
        market_id="mi-senate-r",
        race_key="MI-Senate",
        party_id="R",
        tournament_id=TOURNAMENT_ID,
        side="yes",
        action="buy",
        quantity=100,
        price=0.5,
        idempotency_key="order-1",
    )
    fields.update(overrides)
    return Order(**fields)


@pytest.fixture
def manager(tmp_path):
    limits = RiskLimits(
        max_bankroll_fraction_per_market=1.0,  # not the limit under test
        max_total_exposure_fraction=1.0,  # not the limit under test
        max_party_exposure_fraction=0.5,  # bankroll 1000 -> cap 500
        max_order_size_susqies=1_000_000,
        max_price_deviation_from_fair_value=1.0,
        daily_loss_stop_fraction=1.0,
        stale_data_stop_seconds=60,
    )
    return RiskManager(
        limits=limits,
        bankroll=1000.0,
        event_store=EventStore(tmp_path / "events.db"),
        venue=MockExchange(),
        tournament_id=TOURNAMENT_ID,
        alerter=FakeAlerter(),
        size_ramp=full_size_ramp(),
        fusion_race_keys=frozenset(),
    )


def test_r_yes_and_d_no_in_the_same_race_count_as_the_same_direction(manager):
    # Buying R-YES is a straight long-R bet: 350 notional.
    manager.record_order(
        make_order(
            market_id="mi-senate-r", race_key="MI-Senate", party_id="R",
            side="yes", action="buy", quantity=700, price=0.5, idempotency_key="o1",
        )
    )
    # Buying D-NO in the *same* race is betting against the Democrat, which
    # in a two-party race is the same direction as betting on the
    # Republican: another 200 notional pushing the same way.
    d_no = make_order(
        market_id="mi-senate-d", race_key="MI-Senate", party_id="D",
        side="no", action="buy", quantity=400, price=0.5, idempotency_key="o2",
    )

    # 350 (R-yes) + 200 (D-no, same direction) = 550 > 500 cap.
    decision = manager.check(d_no, fair_value=0.5, outside_data_age_seconds=0)

    assert not decision.approved
    assert "party" in decision.reason.lower()


def test_r_yes_and_d_yes_are_opposite_directions(manager):
    # R-YES (long R) and D-YES (long D) pull the R-vs-D axis in opposite
    # directions, so they should net off rather than stack. Quantities kept
    # small enough that the (generous but finite, fraction=1.0) total and
    # per-market caps never bind — only the party cap is under test here.
    manager.record_order(
        make_order(
            market_id="mi-senate-r", race_key="MI-Senate", party_id="R",
            side="yes", action="buy", quantity=600, price=0.5, idempotency_key="o1",
        )
    )  # +300 net R
    manager.record_order(
        make_order(
            market_id="oh-governor-d", race_key="OH-Governor", party_id="D",
            side="yes", action="buy", quantity=600, price=0.5, idempotency_key="o2",
        )
    )  # -300 net R -> running net = 0

    more_r = make_order(
        market_id="tx-senate-r", race_key="TX-Senate", party_id="R",
        side="yes", action="buy", quantity=600, price=0.5, idempotency_key="o3",
    )  # +300 -> running net = 300, still under the 500 cap
    assert manager.check(more_r, fair_value=0.5, outside_data_age_seconds=0).approved


def test_party_exposure_aggregates_across_different_races(manager):
    # "Total exposure to one party across all races" - R exposure from two
    # unrelated races must still share one national budget.
    manager.record_order(
        make_order(
            market_id="mi-senate-r", race_key="MI-Senate", party_id="R",
            side="yes", action="buy", quantity=600, price=0.5, idempotency_key="o1",
        )
    )  # +300
    fits = make_order(
        market_id="tx-senate-r", race_key="TX-Senate", party_id="R",
        side="yes", action="buy", quantity=380, price=0.5, idempotency_key="o2",
    )  # +190 -> 490, under cap
    assert manager.check(fits, fair_value=0.5, outside_data_age_seconds=0).approved

    manager.record_order(fits)
    too_much = make_order(
        market_id="oh-senate-r", race_key="OH-Senate", party_id="R",
        side="yes", action="buy", quantity=40, price=0.5, idempotency_key="o3",
    )  # +20 -> 510, over cap
    decision = manager.check(too_much, fair_value=0.5, outside_data_age_seconds=0)
    assert not decision.approved


def test_independent_orders_never_trigger_the_party_cap(manager):
    # A huge Independent-market order must never be rejected by the R-vs-D
    # party cap specifically (it's still subject to the per-market and
    # total-exposure caps, which are set generously here to isolate this).
    huge_independent = make_order(
        market_id="ri-governor-i", race_key="RI-Governor", party_id="I",
        side="yes", action="buy", quantity=1800, price=0.5, idempotency_key="o1",
    )  # 900 notional: "huge" relative to the 500 party cap, but still
    # within the deliberately generous (fraction=1.0) market/total caps.
    decision = manager.check(huge_independent, fair_value=0.5, outside_data_age_seconds=0)
    assert decision.approved


def test_independent_orders_are_excluded_from_the_rd_axis(manager):
    # Get right up to the R-vs-D cap...
    manager.record_order(
        make_order(
            market_id="mi-senate-r", race_key="MI-Senate", party_id="R",
            side="yes", action="buy", quantity=1000, price=0.5, idempotency_key="o1",
        )
    )  # +500, exactly at cap

    # ...an Independent order in a *different* race must not push the R
    # axis over, because it isn't counted on that axis at all.
    independent_elsewhere = make_order(
        market_id="ri-governor-i", race_key="RI-Governor", party_id="I",
        side="yes", action="buy", quantity=1000, price=0.5, idempotency_key="o2",
    )
    assert manager.check(independent_elsewhere, fair_value=0.5, outside_data_age_seconds=0).approved

    manager.record_order(independent_elsewhere)
    # And a further R order should still be evaluated as if the
    # Independent order were never there: still exactly at cap, so a small
    # additional R order is still rejected.
    more_r = make_order(
        market_id="tx-senate-r", race_key="TX-Senate", party_id="R",
        side="yes", action="buy", quantity=10, price=0.5, idempotency_key="o3",
    )
    decision = manager.check(more_r, fair_value=0.5, outside_data_age_seconds=0)
    assert not decision.approved


def test_order_without_party_id_is_not_subject_to_the_rd_axis(manager):
    # A non-election market (e.g. an economic indicator) with no party
    # mapping shouldn't be blocked by a limit that doesn't apply to it.
    manager.record_order(
        make_order(quantity=1000, price=0.5, idempotency_key="o1")  # 500, at cap
    )
    no_party_order = make_order(
        market_id="unemployment-rate", race_key=None, party_id=None,
        quantity=1000, price=0.5, idempotency_key="o2",
    )
    assert manager.check(no_party_order, fair_value=0.5, outside_data_age_seconds=0).approved


def test_party_exposure_rejection_is_logged(manager):
    manager.record_order(
        make_order(quantity=1000, price=0.5, idempotency_key="o1")  # at cap: 500
    )
    over = make_order(
        market_id="oh-senate-r", race_key="OH-Senate", party_id="R",
        quantity=20, price=0.5, idempotency_key="o2",
    )

    manager.check(over, fair_value=0.5, outside_data_age_seconds=0)

    events = manager._event_store.all_events(event_type="risk_rejection")
    assert len(events) == 1
    assert events[0]["payload"]["party_id"] == "R"


# --- Fusion races (2026-09-30) ---------------------------------------------
# SIG's rules: a fusion candidate counts for every party on the ticket, so in
# a fusion race R and D can both resolve YES. They are not complements there,
# so a fusion race's R/D orders must not offset anything on the R-vs-D axis.
# Like Independents, they get their own lane (per-market and total caps only).

FUSION_RACE = "NY-Senate"


@pytest.fixture
def fusion_manager(tmp_path):
    limits = RiskLimits(
        max_bankroll_fraction_per_market=1.0,
        max_total_exposure_fraction=1.0,
        max_party_exposure_fraction=0.5,  # bankroll 1000 -> cap 500
        max_order_size_susqies=1_000_000,
        max_price_deviation_from_fair_value=1.0,
        daily_loss_stop_fraction=1.0,
        stale_data_stop_seconds=60,
    )
    return RiskManager(
        limits=limits,
        bankroll=1000.0,
        event_store=EventStore(tmp_path / "events.db"),
        venue=MockExchange(),
        tournament_id=TOURNAMENT_ID,
        alerter=FakeAlerter(),
        size_ramp=full_size_ramp(),
        fusion_race_keys=frozenset({FUSION_RACE}),
    )


def test_fusion_race_position_does_not_offset_rd_exposure_elsewhere(fusion_manager):
    # Long D-YES in a fusion race would read as -400 on the R-vs-D axis and
    # "make room" for 900 of long R elsewhere. It must not.
    fusion_manager.record_order(
        make_order(market_id="ny-senate-d", race_key=FUSION_RACE, party_id="D",
                   quantity=800, price=0.5, idempotency_key="f1")  # fmt: skip
    )
    decision = fusion_manager.check(
        make_order(market_id="mi-senate-r", race_key="MI-Senate", party_id="R",
                   quantity=1800, price=0.5, idempotency_key="f2"),  # fmt: skip
        fair_value=0.5, outside_data_age_seconds=0,
    )
    assert not decision.approved
    assert "party-exposure" in decision.reason


def test_fusion_race_r_and_d_do_not_net_against_each_other(fusion_manager):
    fusion_manager.record_order(
        make_order(market_id="ny-senate-r", race_key=FUSION_RACE, party_id="R",
                   quantity=800, price=0.5, idempotency_key="f3")  # fmt: skip
    )
    fusion_manager.record_order(
        make_order(market_id="ny-senate-d", race_key=FUSION_RACE, party_id="D",
                   quantity=800, price=0.5, idempotency_key="f4")  # fmt: skip
    )
    assert net_rd_exposure(fusion_manager._tracked_orders(), frozenset({FUSION_RACE})) == 0.0
    # ...because both are outside the axis, not because they cancelled:
    only_r = [o for o in fusion_manager._tracked_orders() if o.party_id == "R"]
    assert net_rd_exposure(only_r, frozenset({FUSION_RACE})) == 0.0


def test_fusion_race_orders_never_trigger_the_party_cap(fusion_manager):
    decision = fusion_manager.check(
        make_order(market_id="ny-senate-r", race_key=FUSION_RACE, party_id="R",
                   quantity=1800, price=0.5, idempotency_key="f5"),  # fmt: skip
        fair_value=0.5, outside_data_age_seconds=0,
    )
    assert decision.approved


def test_non_fusion_races_still_net_on_the_axis():
    orders = [
        make_order(race_key="MI-Senate", party_id="R", quantity=100, price=0.5, idempotency_key="a"),
        make_order(race_key="MI-Senate", party_id="D", side="no", quantity=100, price=0.5, idempotency_key="b"),
    ]
    assert net_rd_exposure(orders, frozenset({FUSION_RACE})) == pytest.approx(100.0)


def test_rd_order_without_race_key_is_rejected(fusion_manager):
    # Fail closed: without a race_key the fusion check can't be made.
    decision = fusion_manager.check(make_order(race_key=None, idempotency_key="f6"), fair_value=0.5, outside_data_age_seconds=0)
    assert not decision.approved
    assert "race_key" in decision.reason


def test_risk_manager_requires_fusion_race_keys(tmp_path):
    with pytest.raises(ValueError, match="fusion_race_keys"):
        RiskManager(
            limits=RiskLimits(
                max_bankroll_fraction_per_market=1.0, max_total_exposure_fraction=1.0,
                max_party_exposure_fraction=0.5, max_order_size_susqies=1_000_000,
                max_price_deviation_from_fair_value=1.0, daily_loss_stop_fraction=1.0,
                stale_data_stop_seconds=60,
            ),  # fmt: skip
            bankroll=1000.0,
            event_store=EventStore(tmp_path / "e.db"),
            venue=MockExchange(),
            tournament_id=TOURNAMENT_ID,
            alerter=FakeAlerter(),
            size_ramp=full_size_ramp(),
            fusion_race_keys=None,
        )
