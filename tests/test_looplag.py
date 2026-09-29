"""Loop lag: how late each trading-loop iteration (and the asyncio event
loop itself) runs versus when it was scheduled. Every measurement goes to
events_log; lag above the threshold alerts, rate-limited by a cooldown.
The trading loop must never block — this is how we find out if it does.
"""

import asyncio
import time

import pytest

from predcup.looplag import LoopLagConfig, LoopLagMonitor, load_loop_lag_config
from predcup.store import EventStore


class FakeAlerter:
    def __init__(self):
        self.messages = []

    def send(self, message: str) -> None:
        self.messages.append(message)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


CONFIG = LoopLagConfig(alert_threshold_seconds=1.0, alert_cooldown_seconds=60, probe_interval_seconds=0.5)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def alerter():
    return FakeAlerter()


@pytest.fixture
def store():
    return EventStore(":memory:")


@pytest.fixture
def monitor(store, alerter, clock):
    return LoopLagMonitor(CONFIG, event_store=store, alerter=alerter, clock=clock)


def test_on_time_iteration_logs_zero_lag_and_no_alert(monitor, store, alerter, clock):
    clock.t = 1000.0
    lag = monitor.record("quoter", scheduled_at=1000.0)
    assert lag == 0.0
    [event] = store.all_events(event_type="loop_lag")
    assert event["payload"]["loop"] == "quoter"
    assert event["payload"]["lag_seconds"] == 0.0
    assert alerter.messages == []


def test_early_start_counts_as_zero_lag(monitor, clock):
    clock.t = 999.0
    assert monitor.record("quoter", scheduled_at=1000.0) == 0.0


def test_lag_at_threshold_does_not_alert(monitor, alerter, clock):
    clock.t = 1001.0
    monitor.record("quoter", scheduled_at=1000.0)
    assert alerter.messages == []


def test_lag_over_threshold_alerts(monitor, alerter, clock, store):
    clock.t = 1001.2
    lag = monitor.record("quoter", scheduled_at=1000.0)
    assert lag == pytest.approx(1.2)
    assert len(alerter.messages) == 1
    assert "quoter" in alerter.messages[0]
    assert "1.2" in alerter.messages[0]
    assert store.all_events(event_type="loop_lag")[-1]["payload"]["alerted"] is True


def test_alerts_are_rate_limited_per_loop(monitor, alerter, clock):
    clock.t = 1002.0
    monitor.record("quoter", scheduled_at=1000.0)
    clock.t = 1032.0
    monitor.record("quoter", scheduled_at=1030.0)  # inside 60 s cooldown
    assert len(alerter.messages) == 1
    clock.t = 1070.0
    monitor.record("quoter", scheduled_at=1068.0)  # cooldown over
    assert len(alerter.messages) == 2


def test_cooldown_is_per_loop_name(monitor, alerter, clock):
    clock.t = 1002.0
    monitor.record("quoter", scheduled_at=1000.0)
    monitor.record("reconciliation", scheduled_at=1000.0)
    assert len(alerter.messages) == 2


def test_suppressed_alerts_are_still_logged(monitor, store, clock):
    clock.t = 1002.0
    monitor.record("quoter", scheduled_at=1000.0)
    clock.t = 1005.0
    monitor.record("quoter", scheduled_at=1003.0)
    events = store.all_events(event_type="loop_lag")
    assert len(events) == 2
    assert events[1]["payload"]["alerted"] is False
    assert events[1]["payload"]["over_threshold"] is True


def test_probe_measures_sleep_overshoot(store, alerter, clock):
    async def slow_sleep(seconds):
        clock.t += seconds + 1.5  # event loop was blocked for 1.5 s

    monitor = LoopLagMonitor(CONFIG, event_store=store, alerter=alerter, clock=clock)
    lag = asyncio.run(monitor.probe_once(sleep=slow_sleep))
    assert lag == pytest.approx(1.5)
    assert store.all_events(event_type="loop_lag")[-1]["payload"]["loop"] == "event_loop"
    assert len(alerter.messages) == 1


def test_probe_detects_real_blocking_call(store, alerter):
    # End to end on a real event loop: a synchronous time.sleep in another
    # task delays the probe's wake-up, and the probe reports it.
    config = LoopLagConfig(alert_threshold_seconds=0.05, alert_cooldown_seconds=60, probe_interval_seconds=0.01)
    monitor = LoopLagMonitor(config, event_store=store, alerter=alerter)

    async def blocker():
        await asyncio.sleep(0)
        time.sleep(0.15)  # the kind of call the trading loop must never make

    async def main():
        probe = asyncio.create_task(monitor.probe_once())
        await blocker()
        return await probe

    lag = asyncio.run(main())
    assert lag >= 0.1
    assert len(alerter.messages) == 1


def test_run_probe_loops_until_cancelled(store, alerter):
    config = LoopLagConfig(alert_threshold_seconds=1.0, alert_cooldown_seconds=60, probe_interval_seconds=0.001)
    monitor = LoopLagMonitor(config, event_store=store, alerter=alerter)

    async def main():
        task = asyncio.create_task(monitor.run_probe())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert len(store.all_events(event_type="loop_lag")) >= 2


@pytest.mark.parametrize(
    "overrides",
    [{"alert_threshold_seconds": 0}, {"alert_cooldown_seconds": -1}, {"probe_interval_seconds": 0}],
)
def test_invalid_config_rejected(overrides):
    fields = dict(alert_threshold_seconds=1.0, alert_cooldown_seconds=60, probe_interval_seconds=0.5)
    fields.update(overrides)
    with pytest.raises(ValueError):
        LoopLagConfig(**fields)


def test_shipped_settings_yaml_alerts_above_one_second():
    import yaml

    with open("config/settings.yaml") as f:
        config = load_loop_lag_config(yaml.safe_load(f))
    assert config.alert_threshold_seconds == 1.0


def test_load_fails_loudly_on_missing_section():
    with pytest.raises(KeyError):
        load_loop_lag_config({})
