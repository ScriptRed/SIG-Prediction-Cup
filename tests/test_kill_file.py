"""KILL file in the repo root -> RiskManager.kill(): latch a global halt so
check() rejects every order, then cancel everything (CLAUDE.md Hard Rule 7).
"""

import asyncio

from _helpers import full_size_ramp
from predcup.killfile import KillFileWatcher, load_kill_file_config
from predcup.models import Order
from predcup.risk import RiskLimits, RiskManager
from predcup.store import EventStore
from sim.mock_exchange import MockExchange

TOURNAMENT_ID = "550e8400-e29b-41d4-a716-446655440000"


class FakeAlerter:
    def __init__(self):
        self.messages = []

    def send(self, message: str) -> None:
        self.messages.append(message)


async def no_sleep(_seconds: float) -> None:
    return None


def make_order(**overrides):
    fields = dict(
        exchange_id="36",
        market_id="market-a",
        tournament_id=TOURNAMENT_ID,
        side="yes",
        action="buy",
        quantity=10,
        price=0.5,
        idempotency_key="order-1",
    )
    fields.update(overrides)
    return Order(**fields)


def make_manager(venue, alerter=None):
    limits = RiskLimits(
        max_bankroll_fraction_per_market=1.0,
        max_total_exposure_fraction=1.0,
        max_party_exposure_fraction=1.0,
        max_order_size_susqies=1_000_000,
        max_price_deviation_from_fair_value=1.0,
        daily_loss_stop_fraction=1.0,
        stale_data_stop_seconds=60,
    )
    return RiskManager(
        limits=limits,
        bankroll=1000.0,
        event_store=EventStore(":memory:"),
        venue=venue,
        tournament_id=TOURNAMENT_ID,
        alerter=alerter or FakeAlerter(),
        size_ramp=full_size_ramp(),
        fusion_race_keys=frozenset(),
        sleep=no_sleep,
    )


# --- RiskManager.kill(): the one kill path both triggers call ---------------


def test_kill_latches_halt_so_check_rejects_every_order():
    manager = make_manager(MockExchange())
    assert manager.check(make_order(), fair_value=0.5, outside_data_age_seconds=0).approved

    asyncio.run(manager.kill("test"))

    assert manager.is_killed
    decision = manager.check(make_order(), fair_value=0.5, outside_data_age_seconds=0)
    assert not decision.approved
    assert "kill" in decision.reason.lower()


def test_kill_cancels_all_open_orders_and_alerts():
    venue = MockExchange()
    asyncio.run(venue.place_order(make_order(idempotency_key="o1")))
    asyncio.run(venue.place_order(make_order(idempotency_key="o2", exchange_id="37")))
    alerter = FakeAlerter()
    manager = make_manager(venue, alerter=alerter)

    result = asyncio.run(manager.kill("KILL file"))

    assert result.success
    assert asyncio.run(venue.get_open_orders(TOURNAMENT_ID)) == []
    assert any("KILL file" in m for m in alerter.messages)


def test_kill_is_logged_with_reason():
    manager = make_manager(MockExchange())
    asyncio.run(manager.kill("telegram /kill"))
    events = manager._event_store.all_events(event_type="kill")
    assert events[-1]["payload"]["reason"] == "telegram /kill"


def test_kill_halt_is_latched_even_if_cancel_fails():
    venue = MockExchange()
    placed = asyncio.run(venue.place_order(make_order()))
    venue.configure_cancel_all_to_silently_miss({placed.id})
    alerter = FakeAlerter()
    manager = make_manager(venue, alerter=alerter)

    result = asyncio.run(manager.kill("test"))

    assert not result.success
    assert manager.is_killed
    assert any("FAILED" in m for m in alerter.messages)


# --- KillFileWatcher ---------------------------------------------------------


class Recorder:
    def __init__(self):
        self.reasons = []

    async def __call__(self, reason: str) -> None:
        self.reasons.append(reason)


def test_config_resolves_kill_path_against_repo_root(tmp_path):
    config = {"kill_switch": {"file_path": "KILL", "poll_interval_seconds": 0.5}}
    loaded = load_kill_file_config(config, repo_root=tmp_path)
    assert loaded.path == tmp_path / "KILL"
    assert loaded.poll_interval_seconds == 0.5


def test_no_file_no_kill(tmp_path):
    on_kill = Recorder()
    watcher = KillFileWatcher(tmp_path / "KILL", on_kill, poll_interval_seconds=0.1)
    assert asyncio.run(watcher.check_once()) is False
    assert on_kill.reasons == []


def test_file_appearing_triggers_kill(tmp_path):
    on_kill = Recorder()
    path = tmp_path / "KILL"
    watcher = KillFileWatcher(path, on_kill, poll_interval_seconds=0.1)
    asyncio.run(watcher.check_once())
    path.touch()

    assert asyncio.run(watcher.check_once()) is True
    assert len(on_kill.reasons) == 1
    assert "KILL" in on_kill.reasons[0]


def test_file_present_at_startup_triggers_kill(tmp_path):
    path = tmp_path / "KILL"
    path.touch()
    on_kill = Recorder()
    watcher = KillFileWatcher(path, on_kill, poll_interval_seconds=0.1)
    assert asyncio.run(watcher.check_once()) is True
    assert len(on_kill.reasons) == 1


def test_kill_fires_once_while_file_stays(tmp_path):
    path = tmp_path / "KILL"
    path.touch()
    on_kill = Recorder()
    watcher = KillFileWatcher(path, on_kill, poll_interval_seconds=0.1)
    for _ in range(3):
        asyncio.run(watcher.check_once())
    assert len(on_kill.reasons) == 1


def test_file_removed_and_recreated_fires_again(tmp_path):
    path = tmp_path / "KILL"
    on_kill = Recorder()
    watcher = KillFileWatcher(path, on_kill, poll_interval_seconds=0.1)
    path.touch()
    asyncio.run(watcher.check_once())
    path.unlink()
    asyncio.run(watcher.check_once())
    path.touch()
    asyncio.run(watcher.check_once())
    assert len(on_kill.reasons) == 2


def test_callback_error_is_retried_next_poll(tmp_path):
    """A kill that raises (venue down) must not be marked done: keep trying."""
    path = tmp_path / "KILL"
    path.touch()
    calls = []

    async def flaky(reason: str) -> None:
        calls.append(reason)
        if len(calls) == 1:
            raise RuntimeError("venue unreachable")

    watcher = KillFileWatcher(path, flaky, poll_interval_seconds=0.1)
    asyncio.run(watcher.check_once())
    asyncio.run(watcher.check_once())
    asyncio.run(watcher.check_once())
    assert len(calls) == 2


def test_run_polls_until_file_appears(tmp_path):
    path = tmp_path / "KILL"
    on_kill = Recorder()
    polls = []

    async def fake_sleep(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) == 3:
            path.touch()
        if len(polls) == 5:
            raise asyncio.CancelledError

    watcher = KillFileWatcher(path, on_kill, poll_interval_seconds=0.25, sleep=fake_sleep)
    try:
        asyncio.run(watcher.run())
    except asyncio.CancelledError:
        pass
    assert polls == [0.25] * 5
    assert len(on_kill.reasons) == 1


def test_watcher_wired_to_risk_manager_end_to_end(tmp_path):
    venue = MockExchange()
    asyncio.run(venue.place_order(make_order()))
    manager = make_manager(venue)
    path = tmp_path / "KILL"
    watcher = KillFileWatcher(path, manager.kill, poll_interval_seconds=0.1)

    path.touch()
    asyncio.run(watcher.check_once())

    assert manager.is_killed
    assert asyncio.run(venue.get_open_orders(TOURNAMENT_ID)) == []


def test_repo_settings_have_kill_file_config():
    import yaml

    from predcup.killfile import REPO_ROOT

    with open(REPO_ROOT / "config" / "settings.yaml") as f:
        loaded = load_kill_file_config(yaml.safe_load(f))
    assert loaded.path == REPO_ROOT / "KILL"
