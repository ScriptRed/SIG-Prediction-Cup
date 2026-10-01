"""predcup/health.py: SIG latency stats and the "SIG degraded" switch
(2026-10-01). Degraded = more than 30% of order-book or order requests
timed out in the last 5 minutes (at least 10 requests); recovers at 15% or
when the window empties. Portfolio reads (slow under load) don't count."""

from __future__ import annotations

from predcup.health import HealthConfig, SigHealth
from predcup.store import EventStore

CFG = HealthConfig(window_seconds=300, degraded_fraction=0.30, recover_fraction=0.15, min_requests=10)


class Alerts:
    def __init__(self):
        self.messages = []

    def send(self, m):
        self.messages.append(m)


def make(tmp_path):
    t = {"now": 0.0}
    alerts = Alerts()
    store = EventStore(tmp_path / "e.db")
    return SigHealth(CFG, store, alerts, clock=lambda: t["now"]), t, alerts, store


def feed(h, n, outcome, category="market_data", key="GET /exchanges/prices", seconds=0.1):
    for _ in range(n):
        h.record(key, category, seconds, outcome)


def test_over_30_percent_timeouts_is_degraded_and_alerts_once(tmp_path):
    h, t, alerts, store = make(tmp_path)
    feed(h, 7, "ok")
    feed(h, 3, "timeout", category="orders", key="POST /orders/batch")  # exactly 30%: not over
    assert not h.degraded()
    feed(h, 1, "timeout")  # 4/11 = 36%
    assert h.degraded()
    feed(h, 5, "timeout")
    assert sum("SIG degraded" in m for m in alerts.messages) == 1
    assert store.all_events("sig_degraded")


def test_recovers_at_15_percent_and_alerts(tmp_path):
    h, t, alerts, store = make(tmp_path)
    feed(h, 6, "ok")
    feed(h, 4, "timeout")
    assert h.degraded()
    feed(h, 10, "ok")  # 4/20 = 20%: still degraded (hysteresis)
    assert h.degraded()
    feed(h, 7, "ok")  # 4/27 = 14.8%
    assert not h.degraded()
    assert sum("SIG recovered" in m for m in alerts.messages) == 1


def test_window_expiry_recovers_when_no_requests_are_made(tmp_path):
    h, t, alerts, _ = make(tmp_path)
    feed(h, 6, "ok")
    feed(h, 4, "timeout")
    assert h.degraded()
    t["now"] = 301.0  # quoting pulled: no new requests; old samples expire
    assert not h.degraded()


def test_portfolio_timeouts_and_too_few_requests_dont_count(tmp_path):
    h, t, alerts, _ = make(tmp_path)
    feed(h, 20, "timeout", category="portfolio", key="GET /tournaments/{slug}/portfolio/positions")
    assert not h.degraded()
    feed(h, 3, "timeout")  # 3/3 but under min_requests
    assert not h.degraded()


def test_latency_summary_logged_per_endpoint_and_reset(tmp_path):
    h, t, alerts, store = make(tmp_path)
    for s in (0.1, 0.2, 0.3, 0.4, 10.0):
        h.record("GET /tournaments/{slug}/portfolio/positions", "portfolio", s, "ok")
    h.record("GET /tournaments/{slug}/portfolio/positions", "portfolio", 45.0, "timeout")
    h.record("POST /orders/batch", "orders", 0.05, "http_503")
    h.flush()
    ev = {e["payload"]["endpoint"]: e["payload"] for e in store.all_events("sig_latency")}
    pos = ev["GET /tournaments/{slug}/portfolio/positions"]
    assert (pos["count"], pos["timeouts"], pos["errors"]) == (6, 1, 0)
    assert pos["max_s"] == 45.0 and pos["p50_s"] == 0.3
    assert ev["POST /orders/batch"]["errors"] == 1
    h.flush()  # nothing new: nothing logged
    assert len(store.all_events("sig_latency")) == 2


def test_app_quoter_follows_sig_health(tmp_path):
    from datetime import datetime, timezone

    from test_app import make_app

    h, t, alerts, store = make(tmp_path)
    (tmp_path / "app").mkdir()
    app, _ = make_app(tmp_path / "app", clock=lambda: datetime.now(timezone.utc))
    app.quoter._degraded = h.degraded  # what App does when given health=
    import asyncio

    asyncio.run(app.poll_kalshi_once())
    asyncio.run(app.quote_once())
    assert app.quoter._posted
    feed(h, 6, "ok")
    feed(h, 4, "timeout")
    asyncio.run(app.quote_once())
    assert not app.quoter._posted  # pulled


def test_main_wires_health_into_the_venue_and_app():
    from pathlib import Path

    src = Path("predcup/main.py").read_text()
    assert "on_request=health.record" in src and "health=health" in src
