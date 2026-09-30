"""Indexed events_log reads for /status and the daily summary: they run on
the trading event loop, so no full scans of events_log."""

from datetime import datetime, timedelta, timezone

from predcup.store import EventStore


def test_latest_event_returns_newest_of_that_type():
    store = EventStore(":memory:")
    assert store.latest_event("reconciliation") is None
    store.log("reconciliation", {"status": "clean"})
    store.log("fill", {})
    store.log("reconciliation", {"status": "mismatch"})
    assert store.latest_event("reconciliation")["payload"] == {"status": "mismatch"}


def test_recent_events_newest_first_and_limited():
    store = EventStore(":memory:")
    for i in range(5):
        store.log("loop_lag", {"i": i})
    store.log("alert", {})
    assert [e["payload"]["i"] for e in store.recent_events("loop_lag", limit=3)] == [4, 3, 2]


def test_events_since_filters_by_time_oldest_first():
    store = EventStore(":memory:")
    store.log("alert", {"message": "old"})
    cutoff = datetime.now(timezone.utc) + timedelta(microseconds=1)
    store.log("alert", {"message": "new1"})
    store.log("alert", {"message": "new2"})
    store.log("fill", {})
    got = store.events_since("alert", cutoff - timedelta(microseconds=1))
    assert [e["payload"]["message"] for e in got][-2:] == ["new1", "new2"]
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert store.events_since("alert", future) == []


def test_events_since_accepts_non_utc_aware_datetime():
    store = EventStore(":memory:")
    store.log("alert", {"message": "x"})
    london_summer = timezone(timedelta(hours=1))
    since = (datetime.now(timezone.utc) - timedelta(minutes=5)).astimezone(london_summer)
    assert len(store.events_since("alert", since)) == 1


def test_event_type_index_exists():
    store = EventStore(":memory:")
    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT ts FROM events_log WHERE event_type = ? ORDER BY ts DESC LIMIT 1", ("x",)
    ).fetchall()
    assert any("events_by_type_ts" in str(row) for row in plan)
