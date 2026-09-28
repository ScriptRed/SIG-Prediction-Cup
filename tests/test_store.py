import sqlite3

import pytest

from predcup.store import EventStore


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "events.db")
    yield s
    s.close()


def test_events_log_table_created(store, tmp_path):
    conn = sqlite3.connect(tmp_path / "events.db")
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert "events_log" in tables


def test_wal_mode_enabled(tmp_path):
    store = EventStore(tmp_path / "wal.db")
    conn = sqlite3.connect(tmp_path / "wal.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    store.close()
    assert mode.lower() == "wal"


def test_log_and_read_back_event(store):
    store.log("quote", {"exchange_id": "36", "side": "yes", "price": 0.42})

    events = store.all_events()

    assert len(events) == 1
    assert events[0]["event_type"] == "quote"
    assert events[0]["payload"] == {"exchange_id": "36", "side": "yes", "price": 0.42}
    assert events[0]["ts"]  # timestamp populated


def test_all_events_filters_by_event_type(store):
    store.log("quote", {"a": 1})
    store.log("fill", {"b": 2})
    store.log("quote", {"c": 3})

    quotes = store.all_events(event_type="quote")

    assert len(quotes) == 2
    assert all(e["event_type"] == "quote" for e in quotes)


def test_events_are_ordered_oldest_first(store):
    store.log("first", {})
    store.log("second", {})

    events = store.all_events()

    assert [e["event_type"] for e in events] == ["first", "second"]


def test_log_persists_across_reconnect(tmp_path):
    db_path = tmp_path / "persist.db"
    store1 = EventStore(db_path)
    store1.log("quote", {"x": 1})
    store1.close()

    store2 = EventStore(db_path)
    events = store2.all_events()
    store2.close()

    assert len(events) == 1
    assert events[0]["payload"] == {"x": 1}
