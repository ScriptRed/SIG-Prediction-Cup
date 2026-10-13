"""predcup/apilog.py: every SIG 4xx goes to events_log and the journal
(stderr under systemd) with its full body (2026-10-13 live run: a whole-batch
400 left no details anywhere)."""

from __future__ import annotations

import io
import json

from predcup.apilog import ClientErrorLog
from predcup.store import EventStore

RECORD = {"endpoint": "POST /orders/batch", "status": 400, "code": "VALIDATION_ERROR",
          "message": "Validation failed for one or more orders.",
          "details": {"orders": [{"index": 3, "field": "expirationDate", "message": "must be in the future"}]},
          "body": {"error": {"code": "VALIDATION_ERROR"}}, "retry_after": None,
          "request": {"idempotencyKey": "b", "orders": [{"expirationDate": "2026-10-13T16:08:29Z"}]}}  # fmt: skip


def test_logs_to_events_log_and_journal_in_full(tmp_path):
    store = EventStore(tmp_path / "e.db")
    out = io.StringIO()
    ClientErrorLog(store, stream=out)(RECORD)
    [ev] = store.all_events("sig_client_error")
    assert ev["payload"] == RECORD
    line = out.getvalue().strip()
    assert line.startswith("SIG 4xx ") and "\n" not in line
    assert json.loads(line[len("SIG 4xx "):]) == RECORD  # details and request intact


def test_huge_bodies_are_capped_in_both_places(tmp_path):
    store = EventStore(tmp_path / "e.db")
    out = io.StringIO()
    big = {**RECORD, "body": "x" * 100_000}
    ClientErrorLog(store, stream=out, max_chars=5_000)(big)
    assert len(out.getvalue()) < 6_000
    payload = store.all_events("sig_client_error")[0]["payload"]
    assert payload["details"] == RECORD["details"]  # details are never the part that gets cut
    assert len(json.dumps(payload)) < 6_000


def test_main_wires_the_client_error_log_into_the_sig_venue():
    # amain() needs a live key; check the wiring in its source instead.
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "predcup" / "main.py").read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "SigVenue"]
    assert len(calls) == 1
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert kw.get("on_client_error") == "ClientErrorLog(store)"
