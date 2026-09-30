"""Daily summary to Telegram at 08:00 UK time (Europe/London, so it follows
BST -> GMT on 25 Oct 2026): P&L, fills, markouts, ramp level and alerts in
the last 24 h."""

import asyncio
from datetime import datetime, timedelta, timezone

from predcup.alerts import TelegramAlerter
from predcup.models import Fill
from predcup.status import ReconciliationInfo, StatusSnapshot
from predcup.store import EventStore
from predcup.summary import DailySummaryReporter, load_daily_summary_config, next_run_at

TID = "550e8400-e29b-41d4-a716-446655440000"
UTC = timezone.utc


def at(*args):
    return datetime(*args, tzinfo=UTC)


# events_log stamps rows with the real time, so content tests run "now".
REAL_NOW = datetime.now(UTC)


# --- schedule -----------------------------------------------------------------------


def test_next_run_during_bst_is_0700_utc():
    assert next_run_at(at(2026, 10, 5, 6, 0), "08:00", "Europe/London") == at(2026, 10, 5, 7, 0)


def test_next_run_after_todays_run_is_tomorrow():
    assert next_run_at(at(2026, 10, 5, 7, 0), "08:00", "Europe/London") == at(2026, 10, 6, 7, 0)


def test_next_run_after_clocks_go_back_is_0800_utc():
    # UK clocks go back 02:00 BST on Sunday 25 Oct 2026.
    assert next_run_at(at(2026, 10, 24, 12, 0), "08:00", "Europe/London") == at(2026, 10, 25, 8, 0)
    assert next_run_at(at(2026, 10, 26, 7, 30), "08:00", "Europe/London") == at(2026, 10, 26, 8, 0)


def test_config_from_settings():
    import yaml

    from predcup.killfile import REPO_ROOT

    with open(REPO_ROOT / "config" / "settings.yaml") as f:
        cfg = load_daily_summary_config(yaml.safe_load(f))
    assert cfg.time == "08:00"
    assert cfg.timezone == "Europe/London"


# --- content -------------------------------------------------------------------------


class Alerts:
    def __init__(self):
        self.messages = []

    def send(self, m):
        self.messages.append(m)


class Provider:
    def __init__(self, **overrides):
        fields = dict(
            as_of=at(2026, 10, 5, 7, 0), mode="live", halted=False, halt_reason="",
            ramp_step=2, ramp_max_step=4, ramp_multiplier=0.4, open_orders=4, positions=3,
            pnl_today=1.0, last_reconciliation=ReconciliationInfo("clean", at(2026, 10, 5, 6, 59)), loop_lag={},
        )  # fmt: skip
        fields.update(overrides)
        self.snap = StatusSnapshot(**fields)

    async def snapshot(self):
        return self.snap


def fill(fid, when, qty=10, side="yes", price=0.5, exchange_id="1068"):
    return Fill(id=fid, order_id="o", exchange_id=exchange_id, tournament_id=TID, side=side,
                action="buy", quantity=qty, price=price, filled_at=when)  # fmt: skip


def make_reporter(store, alerter=None, clock=None, pnl_total=None, provider=None):
    return DailySummaryReporter(
        store=store, tournament_id=TID, status_provider=provider or Provider(), alerter=alerter or Alerts(),
        time_of_day="08:00", timezone_name="Europe/London", max_alerts_listed=3,
        pnl_total=pnl_total, clock=clock or (lambda: REAL_NOW),
    )  # fmt: skip


def test_summary_counts_only_last_24h_fills():
    store = EventStore(":memory:")
    store.record_fill(fill("old", REAL_NOW - timedelta(hours=25)))
    store.record_fill(fill("f1", REAL_NOW - timedelta(hours=19), qty=10, exchange_id="1"))
    store.record_fill(fill("f2", REAL_NOW - timedelta(hours=1), qty=5, side="no", exchange_id="2"))
    text = asyncio.run(make_reporter(store).compose())
    assert "Fills: 2 (15 shares, 2 markets)" in text


def test_summary_markouts_by_horizon():
    store = EventStore(":memory:")
    for minutes, value in ((1, 0.01), (1, 0.03), (5, -0.02), (30, 0.0)):
        store.log("markout", {"fill_id": "f", "minutes": minutes, "markout": value})
    text = asyncio.run(make_reporter(store).compose())
    assert "1m +0.020 (n=2)" in text
    assert "5m -0.020 (n=1)" in text
    assert "30m +0.000 (n=1)" in text


def test_summary_without_markouts_says_none():
    text = asyncio.run(make_reporter(EventStore(":memory:")).compose())
    assert "Markouts: none" in text


def test_summary_shows_ramp_and_halt():
    store = EventStore(":memory:")
    text = asyncio.run(make_reporter(store, provider=Provider(halted=True, halt_reason="KILL file")).compose())
    assert "step 2/4" in text and "40%" in text
    assert "HALTED: KILL file" in text


def test_summary_lists_recent_alerts_newest_last_capped():
    store = EventStore(":memory:")
    for i in range(5):
        store.log("alert", {"message": f"alert number {i}"})
    text = asyncio.run(make_reporter(store).compose())
    assert "Alerts (24h): 5" in text
    assert "alert number 4" in text and "alert number 2" in text
    assert "alert number 1" not in text  # capped at 3


def test_summary_excludes_previous_summaries_from_alert_count():
    store = EventStore(":memory:")
    store.log("alert", {"message": "predcup daily summary 2026-10-04\n..."})
    store.log("alert", {"message": "real problem"})
    text = asyncio.run(make_reporter(store).compose())
    assert "Alerts (24h): 1" in text


def test_pnl_total_and_change_since_previous_summary():
    store = EventStore(":memory:")

    async def pnl():
        return 150.0

    store.log("daily_summary", {"date": "2026-10-04", "status": "sent", "pnl_total": 100.0})
    text = asyncio.run(make_reporter(store, pnl_total=pnl).compose())
    assert "P&L: total +150.00, since last summary +50.00" in text


def test_pnl_without_source_is_na():
    text = asyncio.run(make_reporter(EventStore(":memory:")).compose())
    assert "P&L: n/a" in text


# --- sending ------------------------------------------------------------------------


def test_send_if_due_sends_once_per_uk_day():
    store = EventStore(":memory:")
    alerts = Alerts()
    now = {"t": at(2026, 10, 5, 7, 0, 5)}
    reporter = make_reporter(store, alerter=alerts, clock=lambda: now["t"])
    assert asyncio.run(reporter.send_if_due()) is True
    now["t"] += timedelta(minutes=5)
    assert asyncio.run(reporter.send_if_due()) is False
    assert len(alerts.messages) == 1
    assert alerts.messages[0].startswith("predcup daily summary 2026-10-05")
    assert store.latest_event("daily_summary")["payload"]["date"] == "2026-10-05"


def test_not_due_before_0800_uk():
    store = EventStore(":memory:")
    alerts = Alerts()
    reporter = make_reporter(store, alerter=alerts, clock=lambda: at(2026, 10, 5, 6, 59))
    assert asyncio.run(reporter.send_if_due()) is False
    assert alerts.messages == []


def test_restart_later_the_same_day_catches_up_once():
    store = EventStore(":memory:")
    alerts = Alerts()
    reporter = make_reporter(store, alerter=alerts, clock=lambda: at(2026, 10, 5, 15, 0))
    assert asyncio.run(reporter.send_if_due()) is True
    again = make_reporter(store, alerter=alerts, clock=lambda: at(2026, 10, 5, 16, 0))
    assert asyncio.run(again.send_if_due()) is False
    assert len(alerts.messages) == 1


def test_compose_failure_alerts_once_and_does_not_retry_all_day():
    class Broken:
        async def snapshot(self):
            raise RuntimeError("boom")

    store = EventStore(":memory:")
    alerts = Alerts()
    reporter = make_reporter(store, alerter=alerts, provider=Broken(), clock=lambda: at(2026, 10, 5, 7, 1))
    asyncio.run(reporter.send_if_due())
    asyncio.run(reporter.send_if_due())
    assert len(alerts.messages) == 1
    assert "daily summary failed" in alerts.messages[0].lower()


def test_run_checks_at_most_every_minute():
    store = EventStore(":memory:")
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    reporter = DailySummaryReporter(
        store=store, tournament_id=TID, status_provider=Provider(), alerter=Alerts(),
        time_of_day="08:00", timezone_name="Europe/London", max_alerts_listed=3,
        clock=lambda: at(2026, 10, 5, 3, 0), sleep=fake_sleep,
    )  # fmt: skip
    try:
        asyncio.run(reporter.run())
    except asyncio.CancelledError:
        pass
    assert sleeps and all(0 < s <= 60 for s in sleeps)


# --- TelegramAlerter records alerts for the summary --------------------------------------


def test_telegram_alerter_logs_alerts_to_events_log():
    store = EventStore(":memory:")

    async def sender(chat, text):
        return None

    alerter = TelegramAlerter("1", sender, 0, 1, event_store=store)
    alerter.send("something broke")
    assert store.latest_event("alert")["payload"]["message"] == "something broke"
