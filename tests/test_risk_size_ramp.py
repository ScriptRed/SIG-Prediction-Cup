"""Size ramp: order size and per-market limits start at a launch fraction
and step up (×multiplier) only after N clean reconciliations. A
reconciliation mismatch or unexpected 4xx drops one step and alerts; 429s
only drop a step when more than rate_limit_max_in_window arrive within
rate_limit_window_seconds. The step is persisted and a restart resumes one
step below it. RiskManager refuses to run without a ramp (fail closed).
"""

from datetime import datetime, timedelta, timezone

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
T0 = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)


class FakeAlerter:
    def __init__(self):
        self.messages = []

    def send(self, message: str) -> None:
        self.messages.append(message)


class FakeClock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


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


def make_config(**overrides):
    fields = dict(
        launch_fraction=0.1,
        step_multiplier=2.0,
        clean_reconciliations_per_step=3,
        rate_limit_max_in_window=5,
        rate_limit_window_seconds=600,
    )
    fields.update(overrides)
    return SizeRampConfig(**fields)


CONFIG = make_config()


@pytest.fixture
def store():
    return EventStore(":memory:")


@pytest.fixture
def alerter():
    return FakeAlerter()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def ramp(store, alerter, clock):
    return SizeRamp(CONFIG, event_store=store, alerter=alerter, now=clock)


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
    assert max_ramp_step(make_config(launch_fraction=0.25)) == 2
    assert max_ramp_step(make_config(launch_fraction=1.0)) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"launch_fraction": 0.0},
        {"launch_fraction": 1.5},
        {"step_multiplier": 1.0},
        {"clean_reconciliations_per_step": 0},
        {"rate_limit_max_in_window": 0},
        {"rate_limit_window_seconds": 0},
    ],
)
def test_invalid_config_is_rejected(overrides):
    with pytest.raises(ValueError):
        make_config(**overrides)


def test_load_size_ramp_config_from_settings_dict():
    config = {
        "risk": {
            "size_ramp": {
                "launch_fraction": 0.1,
                "step_multiplier": 2.0,
                "clean_reconciliations_per_step": 45,
                "rate_limit_max_in_window": 5,
                "rate_limit_window_seconds": 600,
            }
        }
    }
    assert load_size_ramp_config(config) == make_config(clean_reconciliations_per_step=45)


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
    assert loaded.clean_reconciliations_per_step == 45
    assert loaded.rate_limit_max_in_window == 5
    assert loaded.rate_limit_window_seconds == 600


# --- state machine ----------------------------------------------------------


def test_starts_at_launch_fraction_on_a_fresh_db(ramp):
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


@pytest.mark.parametrize("kind", ["reconciliation_mismatch", "unexpected_4xx"])
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
    ramp.record_failure("unexpected_4xx", "422 on market-a")
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
    ramp.record_failure("unexpected_4xx", "422")

    events = store.all_events(event_type="size_ramp")
    actions = [e["payload"]["action"] for e in events]
    assert actions[0] == "init"
    assert "step_up" in actions
    assert actions[-1] == "step_down"
    last = events[-1]["payload"]
    assert last["step"] == 0
    assert last["multiplier"] == pytest.approx(0.1)
    assert last["failure_kind"] == "unexpected_4xx"
    assert last["detail"] == "422"


def test_each_clean_reconciliation_is_logged_with_progress(ramp, store):
    clean(ramp, 2)
    progress = [
        e["payload"] for e in store.all_events(event_type="size_ramp")
        if e["payload"]["action"] == "clean_reconciliation"
    ]
    assert [p["clean_count"] for p in progress] == [1, 2]


# --- 429 window -------------------------------------------------------------


def test_single_429_alerts_but_does_not_drop_or_reset(ramp, alerter, store):
    clean(ramp, 3 + 2)  # step 1, 2 clean towards step 2
    alerter.messages.clear()

    ramp.record_rate_limited("429 on GET /orders")

    assert ramp.step == 1
    assert ramp.clean_count == 2
    assert len(alerter.messages) == 1
    assert "429" in alerter.messages[0]
    last = store.all_events(event_type="size_ramp")[-1]["payload"]
    assert last["action"] == "rate_limited"
    assert last["rate_limits_in_window"] == 1


def test_five_429s_in_window_do_not_drop(ramp, clock):
    clean(ramp, 3)  # step 1
    for _ in range(5):
        ramp.record_rate_limited("429")
        clock.advance(60)
    assert ramp.step == 1


def test_sixth_429_in_window_drops_one_step(ramp, clock, alerter):
    clean(ramp, 3 * 2)  # step 2
    for _ in range(6):
        ramp.record_rate_limited("429")
        clock.advance(60)
    assert ramp.step == 1
    assert ramp.clean_count == 0
    assert any("rate_limit_burst" in m for m in alerter.messages)


def test_429s_outside_the_window_expire(ramp, clock):
    clean(ramp, 3)  # step 1
    for _ in range(5):
        ramp.record_rate_limited("429")
    clock.advance(601)
    ramp.record_rate_limited("429")  # the first five have aged out
    assert ramp.step == 1


def test_window_clears_after_a_burst_drop(ramp, clock):
    clean(ramp, 3 * 3)  # step 3
    for _ in range(6):
        ramp.record_rate_limited("429")
    assert ramp.step == 2
    ramp.record_rate_limited("429")  # one more: not a second burst on its own
    assert ramp.step == 2


# --- persistence and reset --------------------------------------------------


def test_step_is_persisted_on_every_change(ramp, store):
    clean(ramp, 3 * 2)
    assert store.load_ramp_step() == 2
    ramp.record_failure("unexpected_4xx", "x")
    assert store.load_ramp_step() == 1


def test_restart_resumes_one_step_below_saved(tmp_path, alerter, clock):
    db = tmp_path / "predcup.db"
    first = SizeRamp(CONFIG, event_store=EventStore(db), alerter=alerter, now=clock)
    clean(first, 3 * 3)  # step 3
    assert first.step == 3

    second = SizeRamp(CONFIG, event_store=EventStore(db), alerter=alerter, now=clock)
    assert second.step == 2
    assert second.clean_count == 0
    init = second._event_store.all_events(event_type="size_ramp")[-1]["payload"]
    assert init["action"] == "init"
    assert init["saved_step"] == 3
    assert init["step"] == 2


def test_restart_at_launch_step_stays_at_launch(tmp_path, alerter, clock):
    db = tmp_path / "predcup.db"
    SizeRamp(CONFIG, event_store=EventStore(db), alerter=alerter, now=clock)
    second = SizeRamp(CONFIG, event_store=EventStore(db), alerter=alerter, now=clock)
    assert second.step == 0


def test_restart_resume_is_persisted_so_crash_loops_walk_down(tmp_path, alerter, clock):
    db = tmp_path / "predcup.db"
    first = SizeRamp(CONFIG, event_store=EventStore(db), alerter=alerter, now=clock)
    clean(first, 3 * 3)  # step 3
    for expected in (2, 1, 0, 0):
        assert SizeRamp(CONFIG, event_store=EventStore(db), alerter=alerter, now=clock).step == expected


def test_saved_step_beyond_config_is_clamped(tmp_path, alerter, clock):
    db = tmp_path / "predcup.db"
    EventStore(db).save_ramp_step(9)
    ramp = SizeRamp(CONFIG, event_store=EventStore(db), alerter=alerter, now=clock)
    assert ramp.step == max_ramp_step(CONFIG)  # min(9 - 1, max_step)


def test_manual_reset_returns_to_launch_fraction(ramp, store, alerter):
    clean(ramp, 3 * 3 + 1)
    ramp.reset("manual via /resetramp")
    assert ramp.step == 0
    assert ramp.clean_count == 0
    assert ramp.multiplier == pytest.approx(0.1)
    assert store.load_ramp_step() == 0
    last = store.all_events(event_type="size_ramp")[-1]["payload"]
    assert last["action"] == "reset"
    assert last["detail"] == "manual via /resetramp"
    assert any("reset" in m.lower() for m in alerter.messages)


def test_reset_ramp_script_sets_saved_step_to_launch(tmp_path):
    from scripts.reset_ramp import reset_saved_ramp_step

    db = tmp_path / "predcup.db"
    EventStore(db).save_ramp_step(3)
    reset_saved_ramp_step(db, reason="cli test")
    store = EventStore(db)
    assert store.load_ramp_step() == 0
    assert store.all_events(event_type="size_ramp")[-1]["payload"]["action"] == "reset"


# --- wiring into RiskManager ------------------------------------------------


def make_manager(store, alerter, size_ramp, **limit_overrides):
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


def test_manager_refuses_to_construct_without_a_ramp(store, alerter):
    with pytest.raises(TypeError):
        RiskManager(
            limits=RiskLimits(0.5, 1.0, 1.0, 400, 1.0, 1.0, 60),
            bankroll=1000.0,
            event_store=store,
            venue=MockExchange(),
            tournament_id=TOURNAMENT_ID,
            alerter=alerter,
        )


def test_manager_refuses_a_none_ramp(store, alerter):
    with pytest.raises(ValueError):
        make_manager(store, alerter, size_ramp=None)


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
    for _ in range(3):
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


def test_manager_forwards_429s_to_the_window(store, alerter, ramp):
    manager = make_manager(store, alerter, size_ramp=ramp)
    clean(ramp, 3)
    manager.record_rate_limited("POST /orders", retry_after_seconds=2.0)
    assert ramp.step == 1  # one 429 alone never drops
    last = store.all_events(event_type="size_ramp")[-1]["payload"]
    assert "POST /orders" in last["detail"]
    assert "Retry-After 2s" in last["detail"]
    for _ in range(5):
        manager.record_rate_limited("POST /orders")
    assert ramp.step == 0
