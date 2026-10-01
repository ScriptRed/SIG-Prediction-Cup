"""scripts/shadow_quotes.py (read-only, go-live gate (b)): the latest
intended quote per market from events_log, next to SIG's current best
bid/ask and the Kalshi fair value it was built from."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from predcup.store import EventStore
from scripts import shadow_quotes

T0 = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)
TID = "550e8400-e29b-41d4-a716-446655440000"
SETTINGS = """storage: {db_path: unused.db}
platform: {base_url: "https://sig.test/api/v1", tournament_slug: "cup"}
"""
CUP = """id,exchange_id,title,category,state,office,district,party,race_key
379,1068,D MA Sen,Election Outcome,MA,Senate,,D,MA-Senate
293,983,D TX Sen,Election Outcome,TX,Senate,,D,TX-Senate
294,984,R TX Sen,Election Outcome,TX,Senate,,R,TX-Senate
"""


def put(db: Path, seconds_ago: float, event_type: str, payload: dict) -> None:
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO events_log (ts, event_type, payload) VALUES (?, ?, ?)",
                 ((T0 - timedelta(seconds=seconds_ago)).isoformat(), event_type, json.dumps(payload)))  # fmt: skip
    conn.commit()
    conn.close()


def q(ex, action, price, qty=20, fv=0.615):
    return {"exchange_id": ex, "action": action, "price": price, "quantity": qty, "fair_value": fv, "uncertainty": 0.015}


def setup(tmp_path):
    paths = {"db": tmp_path / "p.db", "settings": tmp_path / "s.yaml", "cup": tmp_path / "cup.csv"}
    EventStore(paths["db"]).close()
    paths["settings"].write_text(SETTINGS)
    paths["cup"].write_text(CUP)
    db = paths["db"]
    put(db, 300, "shadow_quote", q("983", "buy", 0.58))  # older, superseded
    put(db, 30, "shadow_quote", q("983", "buy", 0.60))
    put(db, 30, "shadow_quote", q("983", "sell", 0.63))
    put(db, 20, "shadow_quote", q("1068", "buy", 0.95, fv=0.96))  # bid only
    put(db, 3600 * 3, "shadow_quote", q("984", "buy", 0.30, fv=0.385))  # outside the lookback
    put(db, 10, "fair_value", {"exchange_id": "983", "value": 0.62, "uncertainty": 0.015, "reason": ""})
    put(db, 10, "fair_value", {"exchange_id": "1068", "value": None, "uncertainty": None, "reason": "stale"})
    return paths


def handler_factory(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET", "shadow_quotes must be read-only"
        if request.url.path.endswith("/tournaments/cup"):
            return httpx.Response(200, json={"id": TID})
        assert request.url.path.endswith("/exchanges/prices")
        assert request.url.params["tournamentId"] == TID
        books = {"983": (0.585, 0.665), "1068": (0.94, 0.95)}
        ids = request.url.params["ids"].split(",")
        return httpx.Response(200, json={"data": [{"exchangeId": i, "bestBid": books[i][0], "bestAsk": books[i][1]}
                                                  for i in ids if i in books], "missingIds": []})  # fmt: skip

    return handler


def run(paths, argv, monkeypatch, seen=None):
    monkeypatch.setenv("SIG_API_KEY", "k")
    seen = seen if seen is not None else []
    lines: list[str] = []
    code = shadow_quotes.main(["--db", str(paths["db"]), *argv], out=lines.append, settings_path=paths["settings"],
                              markets_path=paths["cup"], now=lambda: T0,
                              transport=httpx.MockTransport(handler_factory(seen)))  # fmt: skip
    return code, "\n".join(lines)


def row(text, label):
    [line] = [ln for ln in text.splitlines() if ln.startswith(label)]
    return line


def test_latest_quote_per_market_next_to_sig_and_fair_value(tmp_path, monkeypatch):
    code, text = run(setup(tmp_path), [], monkeypatch)
    assert code == 0
    line = row(text, "TX-Senate D")
    assert "0.600 x20" in line and "0.630 x20" in line  # latest bid/ask, not the superseded 0.58
    assert "SIG 0.585/0.665" in line
    assert "FV 0.620" in line  # latest fair_value event
    assert "30s" in line  # quote age


def test_one_sided_quote_and_unavailable_fair_value(tmp_path, monkeypatch):
    _, text = run(setup(tmp_path), [], monkeypatch)
    line = row(text, "MA-Senate D")
    assert "0.950 x20" in line and "  -  " in line
    assert "FV n/a (stale)" in line


def test_flags_a_quote_that_would_cross_sig(tmp_path, monkeypatch):
    # Our 0.95 bid meets SIG's 0.95 ask: in live mode post_only would back off.
    _, text = run(setup(tmp_path), [], monkeypatch)
    assert "CROSSES SIG" in row(text, "MA-Senate D")
    assert "CROSSES" not in row(text, "TX-Senate D")


def test_lookback_excludes_old_quotes(tmp_path, monkeypatch):
    _, text = run(setup(tmp_path), [], monkeypatch)
    assert "TX-Senate R" not in text
    (tmp_path / "wide").mkdir()
    _, text = run(setup(tmp_path / "wide"), ["--minutes", "300"], monkeypatch)
    assert "TX-Senate R" in text


def test_offline_skips_the_sig_read(tmp_path, monkeypatch):
    seen: list = []
    code, text = run(setup(tmp_path), ["--offline"], monkeypatch, seen)
    assert code == 0 and seen == []
    assert "SIG n/a" in row(text, "TX-Senate D")


def test_reads_are_get_only_and_database_is_untouched(tmp_path, monkeypatch):
    paths = setup(tmp_path)
    before = paths["db"].read_bytes()
    seen: list = []
    run(paths, [], monkeypatch, seen)
    assert seen and all(r.method == "GET" for r in seen)
    assert paths["db"].read_bytes() == before


def test_no_quotes_says_so(tmp_path, monkeypatch):
    paths = {"db": tmp_path / "e.db", "settings": tmp_path / "s.yaml", "cup": tmp_path / "cup.csv"}
    EventStore(paths["db"]).close()
    paths["settings"].write_text(SETTINGS)
    paths["cup"].write_text(CUP)
    code, text = run(paths, [], monkeypatch)
    assert code == 0 and "no shadow quotes in the last 60 minutes" in text
