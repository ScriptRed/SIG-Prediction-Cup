"""scripts/today.py: terminal summary of events_log for a time window."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from predcup.store import EventStore
from scripts import today

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

SETTINGS = """storage:
  db_path: "unused.db"
daily_summary:
  timezone: "Europe/London"
"""

CUP = """id,exchange_id,title,category,state,office,district,party,race_key
379,1068,D MA Sen,Election Outcome,MA,Senate,,D,MA-Senate
380,1069,R MA Sen,Election Outcome,MA,Senate,,R,MA-Senate
"""


def put(db: Path, minutes: float, event_type: str, payload: dict) -> None:
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO events_log (ts, event_type, payload) VALUES (?, ?, ?)",
                 ((T0 + timedelta(minutes=minutes)).isoformat(), event_type, json.dumps(payload)))  # fmt: skip
    conn.commit()
    conn.close()


def setup(tmp_path: Path) -> dict[str, Path]:
    paths = {"db": tmp_path / "p.db", "settings": tmp_path / "s.yaml", "cup": tmp_path / "cup.csv"}
    EventStore(paths["db"]).close()
    paths["settings"].write_text(SETTINGS)
    paths["cup"].write_text(CUP)
    db = paths["db"]
    put(db, -120, "fill", {"fill_id": "old", "exchange_id": "1068", "side": "yes", "quantity": 99, "price": 0.5})
    put(db, 1, "fill", {"fill_id": "f1", "exchange_id": "1068", "side": "yes", "quantity": 10, "price": 0.60})
    put(db, 2, "fill", {"fill_id": "f2", "exchange_id": "1068", "side": "no", "quantity": 5, "price": 0.62})
    put(db, 3, "fill", {"fill_id": "f3", "exchange_id": "1069", "side": "yes", "quantity": 20, "price": 0.38})
    for m, mk in ((1, 0.01), (1, 0.03), (5, -0.02), (30, 0.04)):
        put(db, 10, "markout", {"fill_id": "f1", "exchange_id": "1068", "minutes": m, "markout": mk})
    put(db, 10, "markout", {"fill_id": "f3", "exchange_id": "1069", "minutes": 1, "markout": -0.005})
    put(db, 10, "markout_unavailable", {"fill_id": "f3", "exchange_id": "1069", "minutes": 5, "reason": "x"})
    for reason in ("stale outside data", "stale outside data", "exceeds max order size (size ramp at 10%)"):
        put(db, 4, "risk_rejection", {"exchange_id": "1068", "reason": reason})
    put(db, 5, "market_halted", {"market_id": "379", "status_code": 422, "error_code": "POSITION_LIMIT", "message": "m"})
    put(db, 6, "reconciliation", {"status": "mismatch", "detail": "1068 local 10 venue 12"})
    put(db, 6, "reconciliation", {"status": "clean", "positions": 2})
    put(db, 7, "kill", {"reason": "telegram /kill"})
    put(db, 8, "size_ramp", {"action": "step_up", "step": 1, "max_step": 4, "multiplier": 0.2})
    put(db, 9, "size_ramp", {"action": "step_down", "step": 0, "previous_step": 1, "max_step": 4, "multiplier": 0.1,
                             "failure_kind": "reconciliation_mismatch", "detail": "1068"})  # fmt: skip
    put(db, 9, "size_ramp", {"action": "clean_reconciliation", "step": 0})
    put(db, 9, "size_ramp", {"action": "rate_limited", "step": 0, "detail": "GET /orders", "rate_limits_in_window": 1})
    put(db, 9, "size_ramp", {"action": "rate_limited", "step": 0, "detail": "GET /orders", "rate_limits_in_window": 2})
    put(db, 9, "rate_limited_before_risk", {"endpoint": "GET /tournaments/x", "retry_after": 1.0})
    put(db, 600, "fill", {"fill_id": "late", "exchange_id": "1068", "side": "yes", "quantity": 7, "price": 0.5})
    return paths


def run(paths, argv) -> tuple[int, str]:
    lines: list[str] = []
    code = today.main(["--db", str(paths["db"]), *argv], out=lines.append,
                      settings_path=paths["settings"], markets_path=paths["cup"])  # fmt: skip
    return code, "\n".join(lines)


WINDOW = ["--since", T0.isoformat(), "--until", (T0 + timedelta(hours=1)).isoformat()]


def test_fills_in_window_only(tmp_path):
    code, text = run(setup(tmp_path), WINDOW)
    assert code == 0
    assert "Fills: 3 (35 shares, 2 markets)" in text
    assert "old" not in text and "late" not in text
    assert "MA-Senate D" in text  # exchange ids resolved through cup_markets.csv


def test_markouts_averaged_per_market_and_horizon(tmp_path):
    _, text = run(setup(tmp_path), WINDOW)
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("1068"))
    assert "+0.020 (2)" in line  # 1 min: mean of 0.01, 0.03
    assert "-0.020 (1)" in line and "+0.040 (1)" in line
    other = next(ln for ln in text.splitlines() if ln.strip().startswith("1069") and "(1)" in ln)
    assert "-0.005 (1)" in other
    assert "unavailable: 1" in text


def test_risk_rejections_by_reason(tmp_path):
    _, text = run(setup(tmp_path), WINDOW)
    assert "Risk rejections: 3" in text
    assert "2  stale outside data" in text
    assert "1  exceeds max order size (size ramp at 10%)" in text


def test_halts_ramp_changes_and_rate_limits(tmp_path):
    _, text = run(setup(tmp_path), WINDOW)
    assert "market_halted" in text and "POSITION_LIMIT" in text
    assert "reconciliation mismatch" in text and "1068 local 10 venue 12" in text
    assert "kill" in text and "telegram /kill" in text
    assert "Ramp changes: 2" in text
    assert "step_up" in text and "step 1/4 (20%)" in text
    assert "step_down" in text and "reconciliation_mismatch" in text
    assert "clean_reconciliation" not in text
    assert "Rate-limit events: 3" in text
    assert "2  GET /orders" in text and "1  GET /tournaments/x (before risk)" in text
    assert "max 2 in window" in text


def test_empty_window_says_so(tmp_path):
    paths = setup(tmp_path)
    code, text = run(paths, ["--since", (T0 + timedelta(days=5)).isoformat()])
    assert code == 0
    assert "Fills: 0" in text and "Risk rejections: 0" in text and "Halts: none" in text


def test_hours_window_and_read_only(tmp_path):
    paths = setup(tmp_path)
    before = paths["db"].read_bytes()
    code, text = run(paths, ["--hours", "2"])
    assert code == 0
    assert paths["db"].read_bytes() == before
