"""Every SIG 4xx, in full, to events_log (`sig_client_error`) and the
journal (stderr, which systemd captures). Wired as SigVenue's
on_client_error in main.py.

2026-10-13: the first live run halted on a whole-batch 400
VALIDATION_ERROR and neither the journal nor events_log said which order
failed or why. Records carry the response body with error.details and the
request body; only the body/request are shortened if a record is huge.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from predcup.store import EventStore


def _capped(record: dict[str, Any], max_chars: int) -> dict[str, Any]:
    if len(json.dumps(record, default=str)) <= max_chars:
        return record
    out = dict(record)
    for key in ("body", "request"):  # never details, code or message
        if key in out:
            text = json.dumps(out[key], default=str)
            out[key] = text[: max(200, max_chars // 4)] + "...(truncated)"
        if len(json.dumps(out, default=str)) <= max_chars:
            break
    return out


class ClientErrorLog:
    def __init__(self, store: EventStore, *, stream: TextIO | None = None, max_chars: int = 20_000) -> None:
        self._store = store
        self._stream = stream
        self._max_chars = max_chars

    def __call__(self, record: dict[str, Any]) -> None:
        rec = _capped(record, self._max_chars)
        self._store.log("sig_client_error", rec)
        print("SIG 4xx " + json.dumps(rec, default=str), file=self._stream or sys.stderr, flush=True)
