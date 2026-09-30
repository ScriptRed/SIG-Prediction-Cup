"""/status: ramp level, halted or not, open orders, positions count, P&L
today, last reconciliation, loop lag. Data comes through a StatusProvider
(AppStatusProvider is the ready-made one over App); only TELEGRAM_CHAT_ID
gets an answer."""

import asyncio
from datetime import datetime, timedelta, timezone

from predcup.alerts import CommandRouter
from predcup.models import Order
from predcup.status import AppStatusProvider, ReconciliationInfo, StatusSnapshot, format_status
from predcup.store import EventStore
from test_app import TID, BookVenue, make_app

OWNER = "111"
STRANGER = "222"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def snapshot(**overrides):
    fields = dict(
        as_of=NOW,
        mode="live",
        halted=False,
        halt_reason="",
        ramp_step=1,
        ramp_max_step=4,
        ramp_multiplier=0.2,
        open_orders=6,
        positions=3,
        pnl_today=12.5,
        last_reconciliation=ReconciliationInfo(status="clean", at=NOW - timedelta(seconds=42)),
        loop_lag={"event_loop": 0.01, "quoter": 0.35},
    )
    fields.update(overrides)
    return StatusSnapshot(**fields)


class FixedProvider:
    def __init__(self, snap):
        self.snap = snap
        self.calls = 0

    async def snapshot(self):
        self.calls += 1
        return self.snap


def make_router(provider):
    async def kill(reason):
        return None

    return CommandRouter(
        allowed_chat_id=OWNER, on_kill=kill, on_reset_ramp=lambda r: None,
        confirm_timeout_seconds=60, event_store=EventStore(":memory:"), status_provider=provider,
    )  # fmt: skip


# --- format_status ------------------------------------------------------------------


def test_format_shows_every_field():
    text = format_status(snapshot())
    assert "LIVE" in text
    assert "running" in text.lower()
    assert "step 1/4" in text and "20%" in text
    assert "Open orders: 6" in text
    assert "Positions: 3" in text
    assert "+12.50" in text
    assert "clean" in text and "42s ago" in text
    assert "quoter 0.35s" in text


def test_format_halted_shows_reason():
    text = format_status(snapshot(halted=True, halt_reason="telegram /kill"))
    assert "HALTED" in text and "telegram /kill" in text


def test_format_unknown_values_say_so_rather_than_zero():
    text = format_status(snapshot(open_orders=None, positions=None, pnl_today=None,
                                  last_reconciliation=None, loop_lag={}))  # fmt: skip
    assert "Open orders: n/a" in text
    assert "Positions: n/a" in text
    assert "P&L today: n/a" in text
    assert "none yet" in text.lower()
    assert "Open orders: 0" not in text


def test_format_negative_pnl_and_full_size_ramp():
    text = format_status(snapshot(pnl_today=-3.2, ramp_step=4, ramp_multiplier=1.0))
    assert "-3.20" in text
    assert "full size" in text.lower()


# --- /status command -----------------------------------------------------------------


def test_status_command_replies_to_owner():
    provider = FixedProvider(snapshot())
    reply = asyncio.run(make_router(provider).handle(OWNER, "/status"))
    assert "Open orders: 6" in reply


def test_status_from_other_chat_is_ignored_and_provider_not_called():
    provider = FixedProvider(snapshot())
    assert asyncio.run(make_router(provider).handle(STRANGER, "/status")) is None
    assert provider.calls == 0


def test_status_provider_error_is_reported_not_raised():
    class Broken:
        async def snapshot(self):
            raise RuntimeError("db locked")

    reply = asyncio.run(make_router(Broken()).handle(OWNER, "/status"))
    assert "status failed" in reply.lower()


def test_status_without_provider_says_not_wired():
    reply = asyncio.run(make_router(None).handle(OWNER, "/status"))
    assert "not available" in reply.lower()


def test_help_lists_status():
    reply = asyncio.run(make_router(None).handle(OWNER, "/help"))
    assert "/status" in reply


# --- AppStatusProvider ---------------------------------------------------------------


def place(venue, key):
    return asyncio.run(venue.place_order(Order(exchange_id="1068", tournament_id=TID, side="yes", action="buy",
                                               quantity=1, price=0.5, idempotency_key=key)))  # fmt: skip


def test_app_provider_reads_app_state(tmp_path):
    venue = BookVenue()
    app, store = make_app(tmp_path, venue=venue)
    place(venue, "a")
    place(venue, "b")
    store.log("reconciliation", {"status": "clean", "positions": 0})
    store.log("loop_lag", {"loop": "quoter", "lag_seconds": 0.2})
    store.log("loop_lag", {"loop": "event_loop", "lag_seconds": 0.01})
    store.log("loop_lag", {"loop": "quoter", "lag_seconds": 0.05})

    async def pnl():
        return 7.0

    snap = asyncio.run(AppStatusProvider(app, pnl_today=pnl).snapshot())

    assert snap.mode == "shadow"
    assert snap.halted is False
    assert snap.ramp_step == app.ramp.step
    assert snap.ramp_max_step >= snap.ramp_step
    assert snap.open_orders == 2
    assert snap.positions == 0
    assert snap.pnl_today == 7.0
    assert snap.last_reconciliation.status == "clean"
    assert snap.loop_lag == {"quoter": 0.05, "event_loop": 0.01}  # latest per loop


def test_app_provider_reports_halt(tmp_path):
    app, _ = make_app(tmp_path)
    app.request_kill("telegram /kill")
    snap = asyncio.run(AppStatusProvider(app).snapshot())
    assert snap.halted and snap.halt_reason == "telegram /kill"


def test_app_provider_without_pnl_source_reports_none(tmp_path):
    app, _ = make_app(tmp_path)
    snap = asyncio.run(AppStatusProvider(app).snapshot())
    assert snap.pnl_today is None
    assert snap.last_reconciliation is None


def test_app_provider_venue_failure_gives_none_not_error(tmp_path):
    class DownVenue(BookVenue):
        async def get_open_orders(self, tournament_id, exchange_id=None):
            raise ConnectionError("down")

    app, _ = make_app(tmp_path, venue=DownVenue())
    snap = asyncio.run(AppStatusProvider(app).snapshot())
    assert snap.open_orders is None
