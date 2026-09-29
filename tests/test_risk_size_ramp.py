"""Size ramp: order size and per-market limits start at a launch fraction
and step up (×multiplier) only after N clean reconciliations with no
unexpected 4xx / 429 since the last step. Any failure drops back one step
and alerts. Every state change is logged to events_log.
"""

import pytest

from predcup.models import Order
from predcup.risk import (
    RiskLimits,
    RiskManager,
    SizeRamp,
    SizeRampConfig,
    load_size_ramp_config,
    max_ramp_step,
    ramp_multiplier,
)
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
        market_id="market-a",
        tournament_id=TOURNAMENT_ID,
        side="yes",
        action="buy",
        quantity=100,
        price=0.5,
        idempotency_key="order-1",
    )
    fields.update(overrides)
    return Order(**fields)


CONFIG = SizeRampConfig(launch_fraction=0.1, step_multiplier=2.0, clean_reconciliations_per_step=3)


@pytest.fixture
def store():
    return EventStore(":memory:")


@pytest.fixture
def alerter():
    return FakeAlerter()


@pytest.fixture
def ramp(store, alerter):
    return SizeRamp(CONFIG, event_store=store, alerter=alerter)


def clean(ramp, n):
    for _ in range(n):
        ramp.record_clean_reconciliation()


# --- pure maths -------------------------------------------------------------


def test_multiplier_grows_geometrically_and_caps_at_one():
    assert ramp_multiplier(CONFIG, 0) == pytest.approx(0.1)
    assert ramp_multiplier(CONFIG, 1) == pytest.approx(0.2)
    assert ramp_multiplier(CONFIG, 2) == pytest.approx(0.4)
    assert ramp_multiplier(CONFIG, 3) == pytest.approx(0.8)
    assert ramp_multiplier(CONFIG, 4) == 1.0
    assert ramp_multiplier(CONFIG, 10) == 1.0


def test_max_step_is_first_step_at_full_size():
    assert max_ramp_step(CONFIG) == 4
    exact = SizeRampConfig(launch_fraction=0.25, step_multiplier=2.0, clean_reconciliations_per_step=1)
    assert max_ramp_step(exact) == 2
    disabled = SizeRampConfig(launch_fraction=1.0, step_multiplier=2.0, clean_reconciliations_per_step=1)
    assert max_ramp_step(disabled) == 0


@pytest.mark.parametrize(
    "launch_fraction, step_multiplier, n",
    [(0.0, 2.0, 3), (1.5, 2.0, 3), (0.1, 1.0, 3), (0.1, 2.0, 0)],
)
def test_invalid_config_is_rejected(launch_fraction, step_multiplier, n):
    with pytest.raises(ValueError):
        SizeRampConfig(
            launch_fraction=launch_fraction,
            step_multiplier=step_multiplier,
            clean_reconciliations_per_step=n,
        )


def test_load_size_ramp_config_from_settings_dict():
    config = {
        "risk": {
            "size_ramp": {
                "launch_fraction": 0.1,
                "step_multiplier": 2.0,
                "clean_reconciliations_per_step": 120,
            }
        }
    }
    loaded = load_size_ramp_config(config)
    assert loaded == SizeRampConfig(0.1, 2.0, 120)


def test_load_size_ramp_config_fails_loudly_on_missing_key():
    with pytest.raises(KeyError):
        load_size_ramp_config({"risk": {"size_ramp": {"launch_fraction": 0.1}}})
    with pytest.raises(KeyError):
        load_size_ramp_config({"risk": {}})


def test_shipped_settings_yaml_has_a_valid_ramp():
    import yaml

    with open("config/settings.yaml") as f:
        settings = yaml.safe_load(f)
    loaded = load_size_ramp_config(settings)
    assert loaded.launch_fraction < 1.0  # launch must actually be ramped


# --- state machine ----------------------------------------------------------


def test_starts_at_launch_fraction(ramp):
    assert ramp.step == 0
    assert ramp.multiplier == pytest.approx(0.1)
    assert not ramp.at_full_size


def test_steps_up_only_after_n_clean_reconciliations(ramp):
    clean(ramp, 2)
    assert ramp.step == 0
    clean(ramp, 1)
    assert ramp.step == 1
    assert ramp.multiplier == pytest.approx(0.2)
    assert ramp.clean_count == 0  # counter restarts for the next step


def test_climbs_to_full_size_and_stays_there(ramp):
    clean(ramp, 3 * 4)
    assert ramp.step == 4
    assert ramp.at_full_size
    clean(ramp, 30)
    assert ramp.step == 4
    assert ramp.multiplier == 1.0


@pytest.mark.parametrize("kind", ["reconciliation_mismatch", "unexpected_4xx", "rate_limited"])
def test_failure_drops_one_step_resets_count_and_alerts(ramp, alerter, kind):
    clean(ramp, 3 * 2)  # step 2
    clean(ramp, 2)  # partway to step 3
    assert ramp.step == 2

    ramp.record_failure(kind, "detail")

    assert ramp.step == 1
    assert ramp.clean_count == 0
    assert any(kind in m and "step" in m.lower() for m in alerter.messages)


def test_failure_mid_count_blocks_the_step_up(ramp):
    clean(ramp, 2)
    ramp.record_failure("rate_limited", "429 on GET /orders")
    clean(ramp, 1)
    assert ramp.step == 0  # the 2 earlier clean ones no longer count
    clean(ramp, 2)
    assert ramp.step == 1


def test_failure_at_launch_step_stays_at_floor_but_still_alerts(ramp, alerter):
    ramp.record_failure("unexpected_4xx", "422 on market-a")
    assert ramp.step == 0
    assert ramp.multiplier == pytest.approx(0.1)
    assert len(alerter.messages) == 1


def test_unknown_failure_kind_is_rejected(ramp):
    with pytest.raises(ValueError):
        ramp.record_failure("something_else", "x")


def test_state_changes_are_logged(ramp, store):
    clean(ramp, 3)
    ramp.record_failure("rate_limited", "429")

    events = store.all_events(event_type="size_ramp")
    actions = [e["payload"]["action"] for e in events]
    assert actions[0] == "init"
    assert "step_up" in actions
    assert actions[-1] == "step_down"
    last = events[-1]["payload"]
    assert last["step"] == 0
    assert last["multiplier"] == pytest.approx(0.1)
    assert last["failure_kind"] == "rate_limited"
    assert last["detail"] == "429"


def test_each_clean_reconciliation_is_logged_with_progress(ramp, store):
    clean(ramp, 2)
    progress = [
        e["payload"] for e in store.all_events(event_type="size_ramp")
        if e["payload"]["action"] == "clean_reconciliation"
    ]
    assert [p["clean_count"] for p in progress] == [1, 2]


# --- wiring into RiskManager ------------------------------------------------


def make_manager(store, alerter, size_ramp=None, **limit_overrides):
    fields = dict(
        max_bankroll_fraction_per_market=0.5,  # 500 per market at full size
        max_total_exposure_fraction=1.0,
        max_party_exposure_fraction=1.0,
        max_order_size_susqies=400,  # 40 at 10%
        max_price_deviation_from_fair_value=1.0,
        daily_loss_stop_fraction=1.0,
        stale_data_stop_seconds=60,
    )
    fields.update(limit_overrides)
    return RiskManager(
        limits=RiskLimits(**fields),
        bankroll=1000.0,
        event_store=store,
        venue=MockExchange(),
        tournament_id=TOURNAMENT_ID,
        alerter=alerter,
        size_ramp=size_ramp,
    )


def test_ramp_scales_max_order_size(store, alerter, ramp):
    manager = make_manager(store, alerter, size_ramp=ramp)
    assert manager.check(make_order(quantity=80, price=0.5), fair_value=0.5, outside_data_age_seconds=0).approved
    decision = manager.check(make_order(quantity=82, price=0.5), fair_value=0.5, outside_data_age_seconds=0)
    assert not decision.approved
    assert "order size" in decision.reason.lower()
    assert "ramp" in decision.reason.lower()


def test_ramp_scales_per_market_cap(store, alerter, ramp):
    manager = make_manager(store, alerter, size_ramp=ramp, max_order_size_susqies=1_000_000)
    # 10% of the 500 per-market cap = 50
    manager.record_order(make_order(quantity=80, price=0.5, idempotency_key="o1"))  # 40
    assert manager.check(make_order(quantity=20, price=0.5, idempotency_key="o2"), fair_value=0.5, outside_data_age_seconds=0).approved
    decision = manager.check(make_order(quantity=22, price=0.5, idempotency_key="o3"), fair_value=0.5, outside_data_age_seconds=0)
    assert not decision.approved
    assert "per-market" in decision.reason.lower()


def test_ramp_does_not_scale_total_exposure_cap(store, alerter, ramp):
    # Total-exposure / party caps are portfolio-level safety limits, not
    # launch-sizing limits — the ramp only touches order size and per-market.
    manager = make_manager(
        store, alerter, size_ramp=ramp,
        max_order_size_susqies=1_000_000, max_total_exposure_fraction=0.1,
    )
    order = make_order(quantity=100, price=0.5)  # 50: at the ramped market cap, under total cap 100
    assert manager.check(order, fair_value=0.5, outside_data_age_seconds=0).approved


def test_step_up_loosens_limits(store, alerter, ramp):
    manager = make_manager(store, alerter, size_ramp=ramp)
    order = make_order(quantity=160, price=0.5)  # 80: over 40, under 80
    assert not manager.check(order, fair_value=0.5, outside_data_age_seconds=0).approved
    manager.record_reconciliation(matched=True)
    manager.record_reconciliation(matched=True)
    manager.record_reconciliation(matched=True)
    assert manager.check(order, fair_value=0.5, outside_data_age_seconds=0).approved


def test_reconciliation_mismatch_drops_the_ramp(store, alerter, ramp):
    manager = make_manager(store, alerter, size_ramp=ramp)
    clean(ramp, 3)
    manager.record_reconciliation(matched=False, detail="local 100 vs venue 90 on market-a")
    assert ramp.step == 0
    events = store.all_events(event_type="size_ramp")
    assert events[-1]["payload"]["failure_kind"] == "reconciliation_mismatch"


def test_order_rejection_drops_the_ramp(store, alerter, ramp):
    manager = make_manager(store, alerter, size_ramp=ramp)
    clean(ramp, 3)
    manager.record_order_rejection("market-a", 422, "UNKNOWN_LIMIT", "rejected")
    assert ramp.step == 0
    assert manager.is_market_halted("market-a")


def test_rate_limit_drops_the_ramp(store, alerter, ramp):
    manager = make_manager(store, alerter, size_ramp=ramp)
    clean(ramp, 3)
    manager.record_rate_limited("POST /orders", retry_after_seconds=2.0)
    assert ramp.step == 0
    events = store.all_events(event_type="size_ramp")
    assert events[-1]["payload"]["failure_kind"] == "rate_limited"
    assert "POST /orders" in events[-1]["payload"]["detail"]


def test_manager_without_ramp_uses_full_limits(store, alerter):
    manager = make_manager(store, alerter)
    assert manager.check(make_order(quantity=800, price=0.5), fair_value=0.5, outside_data_age_seconds=0).approved
    # these hooks must be safe no-ops without a ramp
    manager.record_reconciliation(matched=True)
    manager.record_rate_limited("GET /orders")
