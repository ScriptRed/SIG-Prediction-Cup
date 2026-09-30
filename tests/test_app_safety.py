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


def test_kill_in_shadow_halts_but_leaves_manual_orders_alone(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, venue=venue)
    resting(venue)
    assert run(app.kill("test")).success
    assert app.control.halted
    assert len(run(venue.get_open_orders(TID))) == 1
    assert store.all_events("kill_switch_shadow")


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
