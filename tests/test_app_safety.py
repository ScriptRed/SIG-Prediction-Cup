"""Safety pieces (merged from the safety branch) wired through App: the
KILL file watcher and Telegram /kill both reach App.kill -> the latched
RiskManager.kill(); /resetramp reaches App.reset_ramp; shutdown cancels
all Cup orders in a finally block (the systemd unit stops with SIGINT)."""

from __future__ import annotations

import asyncio

import pytest

from predcup.alerts import CommandRouter
from predcup.killfile import KillFileWatcher
from predcup.models import Order
from test_app import TID, BookVenue, make_app

CHAT = "12345"


def run(c):
    return asyncio.run(c)


def resting(venue, key="manual-1"):
    return run(venue.place_order(Order(exchange_id="1068", tournament_id=TID, side="yes", action="buy",
                                       quantity=1, price=0.5, idempotency_key=key)))  # fmt: skip


def test_kill_live_latches_risk_and_cancels_everything(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    resting(venue)
    result = run(app.kill("test"))
    assert result.success
    assert app.risk.is_killed and app.control.halted
    assert run(venue.get_open_orders(TID)) == []
    order = Order(exchange_id="1068", market_id="379", tournament_id=TID, party_id="D", race_key="MA-Senate",
                  side="yes", action="buy", quantity=1, price=0.5, idempotency_key="after")  # fmt: skip
    assert app.risk.check(order, fair_value=0.5, outside_data_age_seconds=0).reason == "kill switch engaged"


def test_kill_in_shadow_also_cancels_everything(tmp_path):
    # One rule (2026-10-01): kill means no open Cup orders, in any mode.
    venue = BookVenue()
    app, store = make_app(tmp_path, venue=venue)
    resting(venue)
    assert run(app.kill("test")).success
    assert app.control.halted and app.risk.is_killed
    assert run(venue.get_open_orders(TID)) == []
    assert store.all_events("kill")[0]["payload"]["reason"] == "test"


def test_internal_halt_in_shadow_cancels_everything(tmp_path):
    venue = BookVenue()
    app, _ = make_app(tmp_path, venue=venue)
    resting(venue)
    app.control.halt("reconciliation mismatch: 1068")
    run(app.handle_halt_once())
    assert run(venue.get_open_orders(TID)) == []


class StubbornVenue(BookVenue):
    async def cancel_all(self, tournament_id, exchange_id=None, market_id=None):
        from predcup.venues.base import CancelAllResult

        return CancelAllResult(cancelled=0, remaining=0)  # claims success, cancels nothing


def test_kill_verifies_and_alerts_when_orders_remain(tmp_path):
    venue = StubbornVenue()
    app, _ = make_app(tmp_path, venue=venue)
    resting(venue)
    result = run(app.kill("test"))
    assert not result.success and result.remaining_order_ids
    assert any("still open" in m for m in app.alerter.messages)


def test_second_kill_is_harmless(tmp_path):
    app, _ = make_app(tmp_path, shadow=False, live_allowed=True)
    run(app.kill("one"))
    assert run(app.kill("two")).success


def test_internal_halt_in_live_goes_through_the_latched_kill(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    resting(venue)
    app.control.halt("reconciliation mismatch: 1068")
    run(app.handle_halt_once())
    assert app.risk.is_killed
    assert run(venue.get_open_orders(TID)) == []
    assert store.all_events("kill")[0]["payload"]["reason"] == "reconciliation mismatch: 1068"


def test_kill_file_watcher_kills_the_app(tmp_path):
    venue = BookVenue()
    app, _ = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    resting(venue)
    kill_file = tmp_path / "KILL"
    watcher = KillFileWatcher(kill_file, app.kill, poll_interval_seconds=0.01)
    assert run(watcher.check_once()) is False
    kill_file.touch()
    assert run(watcher.check_once()) is True
    assert app.risk.is_killed and run(venue.get_open_orders(TID)) == []


def test_telegram_kill_and_resetramp_reach_the_app(tmp_path):
    app, store = make_app(tmp_path, shadow=False, live_allowed=True)
    router = CommandRouter(CHAT, on_kill=app.kill, on_reset_ramp=app.reset_ramp,
                           confirm_timeout_seconds=60, event_store=store)  # fmt: skip
    assert run(router.handle("999", "/kill")) is None  # other chats ignored
    assert not app.control.halted
    run(router.handle(CHAT, "/resetramp"))
    assert run(router.handle(CHAT, "YES")) == "Size ramp reset to launch size."
    assert any(e["payload"].get("action") == "reset" for e in store.all_events("size_ramp"))
    reply = run(router.handle(CHAT, "/kill"))
    assert reply.startswith("Kill engaged") and app.risk.is_killed


def test_serve_cancels_all_cup_orders_on_shutdown_in_live(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    resting(venue)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(app.serve(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    run(go())
    assert run(venue.get_open_orders(TID)) == []
    assert store.all_events("shutdown")[0]["payload"]["cancel_all"] == "clean"


def test_serve_cancels_even_when_cancelled_like_a_crash(tmp_path):
    venue = BookVenue()
    app, _ = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    resting(venue)

    async def go():
        task = asyncio.create_task(app.serve(asyncio.Event()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    assert run(venue.get_open_orders(TID)) == []


def test_serve_in_shadow_cancels_nothing_on_shutdown(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, venue=venue)
    resting(venue)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(app.serve(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    run(go())
    assert len(run(venue.get_open_orders(TID))) == 1
    assert store.all_events("shutdown")[0]["payload"]["cancel_all"] == "skipped (shadow)"


def test_main_calls_shutdown_in_a_finally_block():
    import ast
    from pathlib import Path

    tree = ast.parse(Path("predcup/app.py").read_text())
    serve = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "serve")
    tries = [n for n in ast.walk(serve) if isinstance(n, ast.Try) and n.finalbody]
    assert any("shutdown" in ast.unparse(t.finalbody[i]) for t in tries for i in range(len(t.finalbody)))


# --- session 2 pieces: watchdog, startup cancel-all, /status, daily summary -------------


class FakeWatchdog:
    def __init__(self):
        self.beats: list[str] = []
        self.ran = False

    def beat(self, loop):
        self.beats.append(loop)

    async def run(self):
        self.ran = True
        await asyncio.sleep(3600)


def test_watchdog_is_only_built_under_systemd():
    from predcup.main import build_watchdog

    settings = {"watchdog": {"loops": ["quoter"], "max_silence_seconds": 180, "default_ping_interval_seconds": 20}}
    assert build_watchdog(settings, alerter=None, env={}) is None  # laptop: no NOTIFY_SOCKET
    assert build_watchdog(settings, alerter=None, env={"NOTIFY_SOCKET": "/run/systemd/notify"}) is not None


def test_loops_beat_the_watchdog(tmp_path):
    from datetime import datetime, timezone

    app, _ = make_app(tmp_path, clock=lambda: datetime.now(timezone.utc))
    wd = FakeWatchdog()
    app.set_watchdog(wd)
    run(app.run(duration_seconds=0.3))
    assert {"quoter", "kalshi_poll", "reconciliation"} <= set(wd.beats) and wd.ran


def test_no_watchdog_is_fine(tmp_path):
    app, store = make_app(tmp_path)
    run(app.run(duration_seconds=0.2))
    assert store.all_events("loop_lag")


def _serve_briefly(app):
    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(app.serve(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    run(go())


def test_serve_cancels_leftover_orders_on_startup_in_live(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    resting(venue, "left-by-a-crashed-process")
    _serve_briefly(app)
    assert store.all_events("startup_cancel_all")[0]["payload"]["result"] == "clean"
    events = [e["event_type"] for e in store.all_events() if e["event_type"] in ("startup_cancel_all", "loop_lag")]
    assert events[0] == "startup_cancel_all"  # before any loop ran


def test_serve_skips_startup_cancel_in_shadow(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, venue=venue)
    resting(venue, "manual")
    _serve_briefly(app)
    assert store.all_events("startup_cancel_all")[0]["payload"]["result"] == "skipped (shadow)"


class PnlVenue(BookVenue):
    async def get_pnl(self, tournament_id, period):
        from predcup.venues.sig import TournamentPnl

        return TournamentPnl(period=period, period_pnl={"day": 12.5, "all": 340.0}[period], unrealized_pnl=0.0,
                             total_account_value=100_340.0, roi=None)  # fmt: skip


def test_status_provider_shows_todays_cup_pnl(tmp_path):
    from predcup.status import format_status

    app, _ = make_app(tmp_path, venue=PnlVenue())
    text = format_status(run(app.status_provider().snapshot()))
    assert "P&L today: +12.50" in text and "SHADOW" in text


def test_status_provider_pnl_unreadable_is_na(tmp_path):
    from predcup.status import format_status

    class FailingPnlVenue(BookVenue):
        async def get_pnl(self, tournament_id, period):
            raise TimeoutError("SIG slow")

    app, _ = make_app(tmp_path, venue=FailingPnlVenue())
    assert "P&L today: n/a" in format_status(run(app.status_provider().snapshot()))


def test_telegram_status_command_uses_the_app_provider(tmp_path):
    app, store = make_app(tmp_path, venue=PnlVenue())
    router = CommandRouter(CHAT, on_kill=app.kill, on_reset_ramp=app.reset_ramp, confirm_timeout_seconds=60,
                           event_store=store, status_provider=app.status_provider())  # fmt: skip
    assert "P&L today: +12.50" in run(router.handle(CHAT, "/status"))


def test_daily_summary_uses_total_cup_pnl(tmp_path):
    app, _ = make_app(tmp_path, venue=PnlVenue())
    reporter = app.daily_summary(alerter=app.alerter)
    text = run(reporter.compose())
    assert "P&L: total +340.00" in text


# --- audit 2026-10-01 H1: a failed kill must be retried, not reported as done ----------


class FlakyCancelVenue(BookVenue):
    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    async def cancel_all(self, tournament_id, exchange_id=None, market_id=None):
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("venue unreachable")
        return await super().cancel_all(tournament_id, exchange_id, market_id)


def test_kill_that_raised_is_retried_by_the_next_kill(tmp_path):
    venue = FlakyCancelVenue(failures=1)
    app, _ = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    resting(venue)
    with pytest.raises(ConnectionError):
        run(app.kill("KILL file"))
    result = run(app.kill("KILL file"))  # the watcher's retry on its next poll
    assert result.success and result.attempts >= 1
    assert run(venue.get_open_orders(TID)) == []


def test_kill_file_watcher_retries_until_orders_are_gone(tmp_path):
    venue = FlakyCancelVenue(failures=1)
    app, _ = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    resting(venue)
    kill_file = tmp_path / "KILL"
    kill_file.touch()
    watcher = KillFileWatcher(kill_file, app.kill, poll_interval_seconds=0.01)
    assert run(watcher.check_once()) is False  # raised: not marked done
    assert run(watcher.check_once()) is True
    assert run(venue.get_open_orders(TID)) == []


class CountingStubbornVenue(StubbornVenue):
    def __init__(self):
        super().__init__()
        self.cancel_calls = 0

    async def cancel_all(self, tournament_id, exchange_id=None, market_id=None):
        self.cancel_calls += 1
        return await super().cancel_all(tournament_id, exchange_id, market_id)


def test_failed_kill_result_is_retried_not_cached(tmp_path):
    venue = CountingStubbornVenue()  # claims success, cancels nothing
    app, _ = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    resting(venue)
    assert not run(app.kill("first")).success
    calls = venue.cancel_calls
    assert not run(app.kill("second")).success
    assert venue.cancel_calls > calls  # really tried again


def test_successful_kill_is_not_repeated(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)
    assert run(app.kill("one")).success
    assert run(app.kill("two")).success
    assert len(store.all_events("kill")) == 1


# --- audit 2026-10-01 H2: kill while a re-quote is in flight ------------------------------


class GatedBatchVenue(BookVenue):
    """place_batch waits until released: the kill lands mid-flight."""

    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def place_batch(self, orders, batch_key):
        self.entered.set()
        await self.release.wait()
        return await super().place_batch(orders, batch_key)


def test_kill_during_an_in_flight_batch_leaves_no_open_orders(tmp_path):
    venue = GatedBatchVenue()
    app, store = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue)

    async def go():
        await app.reconcile_once()  # live mode needs a fresh daily P&L before any order (fail closed)
        await app.poll_kalshi_once()
        quoting = asyncio.create_task(app.quote_once())
        await venue.entered.wait()  # risk-checked, batch in flight
        killing = asyncio.create_task(app.kill("Telegram /kill"))
        await asyncio.sleep(0.05)  # kill's own cancel-all runs before the batch lands
        venue.release.set()
        result = await killing
        await quoting
        return result

    result = run(go())
    assert result.success
    assert run(venue.get_open_orders(TID)) == []


# --- 2026-10-01 launch: an internal halt's kill is retried until clean ------------------


class TimeoutCancelVenue(BookVenue):
    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    async def cancel_all(self, tournament_id, exchange_id=None, market_id=None):
        tournament_wide = exchange_id is None and market_id is None  # the kill's sweep, not a re-quote
        if tournament_wide and self.failures > 0:
            self.failures -= 1
            import httpx

            raise httpx.ReadTimeout("SIG slow")
        return await super().cancel_all(tournament_id, exchange_id, market_id)


def test_internal_halt_kill_is_retried_after_a_timeout(tmp_path):
    from datetime import datetime, timezone

    venue = TimeoutCancelVenue(failures=2)
    app, store = make_app(tmp_path, shadow=False, live_allowed=True, venue=venue,
                          clock=lambda: datetime.now(timezone.utc))  # fmt: skip
    resting(venue)

    async def go():
        task = asyncio.create_task(app.run(duration_seconds=4.0))  # retries at +1 s, +2 s
        await asyncio.sleep(0.05)
        app.control.halt("reconciliation mismatch: 1068")  # internal halt, no external retrier
        await task

    run(go())
    assert run(venue.get_open_orders(TID)) == []
    assert len(store.all_events("kill_retry")) == 2
