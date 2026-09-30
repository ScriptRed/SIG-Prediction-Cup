"""systemd watchdog (deploy/predcup.service WatchdogSec): loops beat() every
iteration; the ping task sends WATCHDOG=1 only while every expected loop is
fresh, so a hung loop or a blocked event loop gets the bot restarted. On
startup the bot cancels all Cup orders (live) before anything else."""

import asyncio
import socket

import pytest

from predcup.models import Order
from predcup.watchdog import (
    Watchdog,
    cancel_all_on_startup,
    load_watchdog_config,
    notify_socket_address,
    sd_notify,
    watchdog_ping_interval,
)
from test_app import TID, BookVenue, make_app


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Notes:
    def __init__(self):
        self.sent = []

    def __call__(self, message: str) -> bool:
        self.sent.append(message)
        return True


class FakeAlerter:
    def __init__(self):
        self.messages = []

    def send(self, m):
        self.messages.append(m)


def make_watchdog(clock=None, notes=None, alerter=None, loops=("quoter", "reconciliation")):
    return Watchdog(
        loops=loops,
        max_silence_seconds=60,
        ping_interval_seconds=10,
        notify=notes or Notes(),
        alerter=alerter or FakeAlerter(),
        clock=clock or Clock(),
    )


# --- sd_notify -------------------------------------------------------------------


def test_sd_notify_is_noop_without_notify_socket():
    assert sd_notify("WATCHDOG=1", env={}) is False


def test_sd_notify_sends_datagram_to_notify_socket(tmp_path):
    path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(path)
    try:
        assert sd_notify("WATCHDOG=1", env={"NOTIFY_SOCKET": path}) is True
        assert server.recv(64) == b"WATCHDOG=1"
    finally:
        server.close()


def test_abstract_namespace_socket_address():
    assert notify_socket_address("@/org/systemd/notify") == "\0/org/systemd/notify"
    assert notify_socket_address("/run/systemd/notify") == "/run/systemd/notify"


def test_sd_notify_failure_returns_false(tmp_path):
    assert sd_notify("WATCHDOG=1", env={"NOTIFY_SOCKET": str(tmp_path / "missing.sock")}) is False


def test_ping_interval_is_a_third_of_watchdog_usec():
    assert watchdog_ping_interval({"WATCHDOG_USEC": "60000000"}, default=5) == pytest.approx(20)
    assert watchdog_ping_interval({}, default=5) == 5


def test_ping_interval_ignores_watchdog_meant_for_another_pid():
    env = {"WATCHDOG_USEC": "60000000", "WATCHDOG_PID": "1"}
    assert watchdog_ping_interval(env, default=5, pid=4242) == 5


# --- Watchdog ------------------------------------------------------------------------


def test_first_tick_sends_ready_then_watchdog():
    notes = Notes()
    wd = make_watchdog(notes=notes)
    assert wd.tick() is True
    assert notes.sent == ["READY=1", "WATCHDOG=1"]


def test_all_loops_fresh_pings():
    clock, notes = Clock(), Notes()
    wd = make_watchdog(clock=clock, notes=notes)
    wd.tick()
    clock.now += 50
    wd.beat("quoter")
    wd.beat("reconciliation")
    clock.now += 50
    assert wd.tick() is True
    assert notes.sent.count("WATCHDOG=1") == 2


def test_loop_that_never_beats_goes_stale_after_max_silence_from_start():
    clock, notes = Clock(), Notes()
    wd = make_watchdog(clock=clock, notes=notes)
    wd.tick()
    clock.now += 61
    wd.beat("quoter")
    assert wd.stale_loops() == ["reconciliation"]
    assert wd.tick() is False
    assert notes.sent.count("WATCHDOG=1") == 1


def test_hung_loop_withholds_ping_and_alerts_once():
    clock, notes, alerter = Clock(), Notes(), FakeAlerter()
    wd = make_watchdog(clock=clock, notes=notes, alerter=alerter)
    wd.beat("quoter")
    wd.beat("reconciliation")
    wd.tick()
    for _ in range(3):
        clock.now += 30
        wd.beat("reconciliation")  # quoter hung
        wd.tick()
    assert notes.sent.count("WATCHDOG=1") == 3  # quoter silent 0, 30, 60 s ok; 90 s stale
    assert len(alerter.messages) == 1
    assert "quoter" in alerter.messages[0]


def test_recovered_loop_resumes_pings_and_rearms_alert():
    clock, notes, alerter = Clock(), Notes(), FakeAlerter()
    wd = make_watchdog(clock=clock, notes=notes, alerter=alerter)
    wd.tick()
    clock.now += 61
    wd.tick()
    wd.beat("quoter")
    wd.beat("reconciliation")
    assert wd.tick() is True
    clock.now += 61
    wd.tick()
    assert len(alerter.messages) == 2


def test_unknown_loop_beat_is_ignored_not_required():
    wd = make_watchdog()
    wd.beat("something_else")
    assert "something_else" not in wd.stale_loops()


def test_run_pings_until_cancelled_then_sends_stopping():
    notes = Notes()
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    wd = Watchdog(loops=(), max_silence_seconds=60, ping_interval_seconds=7, notify=notes,
                  alerter=FakeAlerter(), clock=Clock(), sleep=fake_sleep)  # fmt: skip
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(wd.run())
    assert sleeps == [7, 7, 7]
    assert notes.sent[0] == "READY=1"
    assert notes.sent.count("WATCHDOG=1") == 3
    assert notes.sent[-1] == "STOPPING=1"


def test_config_from_settings():
    import yaml

    from predcup.killfile import REPO_ROOT

    with open(REPO_ROOT / "config" / "settings.yaml") as f:
        cfg = load_watchdog_config(yaml.safe_load(f))
    assert set(cfg.loops) == {"kalshi_poll", "quoter", "reconciliation"}
    assert cfg.max_silence_seconds > 60  # longer than the slowest loop's interval


# --- startup cancel-all ------------------------------------------------------------


def place(venue, key):
    return asyncio.run(venue.place_order(Order(exchange_id="1068", tournament_id=TID, side="yes", action="buy",
                                               quantity=1, price=0.5, idempotency_key=key)))  # fmt: skip


def test_startup_cancels_all_cup_orders_in_live_mode(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    place(venue, "left-over-1")
    place(venue, "left-over-2")

    result = asyncio.run(cancel_all_on_startup(app))

    assert result.success
    assert asyncio.run(venue.get_open_orders(TID)) == []
    assert store.all_events("startup_cancel_all")[-1]["payload"]["result"] == "clean"
    assert not app.control.halted


def test_startup_cancel_failure_halts_and_alerts(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    placed = place(venue, "stuck")
    venue.configure_cancel_all_to_silently_miss({placed.id})

    result = asyncio.run(cancel_all_on_startup(app, retry_delay_seconds=0))

    assert not result.success
    assert app.control.halted
    assert "startup" in app.control.reason


def test_startup_in_shadow_mode_leaves_manual_orders(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, venue=venue)
    place(venue, "manual")

    assert asyncio.run(cancel_all_on_startup(app)) is None
    assert len(asyncio.run(venue.get_open_orders(TID))) == 1
    assert store.all_events("startup_cancel_all")[-1]["payload"]["result"] == "skipped (shadow)"
