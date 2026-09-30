"""scripts/bot_status.py: read-only checks for the restart-after-kill
procedure (docs/deploy.md)."""

from __future__ import annotations

from predcup.store import EventStore
from scripts.bot_status import latest, status_lines


def test_latest_returns_newest_event_of_each_type(tmp_path):
    store = EventStore(tmp_path / "e.db")
    store.log("reconciliation", {"status": "mismatch"})
    store.log("reconciliation", {"status": "clean"})
    assert latest(store, "reconciliation")["payload"]["status"] == "clean"
    assert latest(store, "kill") is None


def test_status_lines_flag_open_orders_and_unclean_reconciliation(tmp_path):
    store = EventStore(tmp_path / "e.db")
    store.log("app_start", {"shadow": False})
    store.log("reconciliation", {"status": "mismatch", "detail": "1077: local 1 vs venue 2"})
    lines, ok = status_lines(store, open_order_ids=["55"], kill_file_present=True)
    text = "\n".join(lines)
    assert not ok
    assert "KILL file present" in text and "1 open Cup order" in text and "mismatch" in text


def test_status_ok_after_clean_restart(tmp_path):
    store = EventStore(tmp_path / "e.db")
    store.log("kill", {"reason": "telegram /kill"})
    store.log("app_start", {"shadow": False})
    store.log("reconciliation", {"status": "clean", "positions": 0})
    lines, ok = status_lines(store, open_order_ids=[], kill_file_present=False)
    assert ok, lines
