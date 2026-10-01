"""SIG health: per-endpoint latency stats and the "SIG degraded" switch.

SigVenue reports every request attempt here (on_request). Two uses:

- Latency: flush() writes one `sig_latency` event per endpoint (count,
  p50/p95/max seconds, timeouts, errors) for what was seen since the last
  flush; App flushes every health.latency_log_interval_seconds, so
  events_log shows how SIG behaves over the day without a row per request.
- Degraded: if more than degraded_fraction of order-book/price and order
  requests (portfolio reads are slow under load and don't count) timed out
  within window_seconds, with at least min_requests in the window, SIG is
  "degraded": the quoter pulls every quote and posts nothing, and a person
  is alerted. It recovers (alert, quoting resumes) when the timeout share
  falls to recover_fraction or the window empties; the gap stops flapping.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from predcup.risk import Alerter
from predcup.store import EventStore

WATCHED = ("market_data", "orders")


@dataclass(frozen=True)
class HealthConfig:
    window_seconds: float
    degraded_fraction: float
    recover_fraction: float
    min_requests: int


def load_health_config(settings: dict) -> HealthConfig:
    h = settings["sig_health"]
    return HealthConfig(
        window_seconds=float(h["window_seconds"]),
        degraded_fraction=float(h["degraded_timeout_fraction"]),
        recover_fraction=float(h["recover_timeout_fraction"]),
        min_requests=int(h["min_requests"]),
    )


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    i = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
    return sorted_values[i]


class SigHealth:
    def __init__(
        self,
        cfg: HealthConfig,
        store: EventStore,
        alerter: Alerter,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._alerter = alerter
        self._clock = clock
        self._window: deque[tuple[float, bool]] = deque()  # (time, timed out) for watched categories
        self._is_degraded = False
        self._stats: dict[str, dict] = {}

    # --- observation (SigVenue on_request) -------------------------------------

    def record(self, key: str, category: str, seconds: float, outcome: str) -> None:
        s = self._stats.setdefault(key, {"category": category, "latencies": [], "timeouts": 0, "errors": 0})
        s["latencies"].append(seconds)
        if outcome == "timeout":
            s["timeouts"] += 1
        elif outcome != "ok":
            s["errors"] += 1
        if category in WATCHED:
            self._window.append((self._clock(), outcome == "timeout"))
            self._evaluate()

    # --- degraded switch -----------------------------------------------------------

    def _prune(self) -> None:
        cutoff = self._clock() - self._cfg.window_seconds
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

    def _evaluate(self) -> None:
        self._prune()
        total = len(self._window)
        timeouts = sum(1 for _, t in self._window if t)
        share = timeouts / total if total else 0.0
        if not self._is_degraded:
            if total >= self._cfg.min_requests and share > self._cfg.degraded_fraction + 1e-12:
                self._is_degraded = True
                self._store.log("sig_degraded", {"timeouts": timeouts, "requests": total, "share": round(share, 3)})
                self._alerter.send(
                    f"SIG degraded: {timeouts}/{total} order-book/order requests timed out in "
                    f"{self._cfg.window_seconds:.0f} s. All quotes pulled; resumes automatically on recovery."
                )
        elif total < self._cfg.min_requests or share <= self._cfg.recover_fraction + 1e-12:
            self._is_degraded = False
            self._store.log("sig_recovered", {"timeouts": timeouts, "requests": total, "share": round(share, 3)})
            self._alerter.send(f"SIG recovered ({timeouts}/{total} timeouts in the window): quoting resumes.")

    def degraded(self) -> bool:
        if self._is_degraded:
            self._evaluate()  # the window may have emptied with no new requests
        return self._is_degraded

    # --- latency log --------------------------------------------------------------------

    def flush(self) -> None:
        for key, s in self._stats.items():
            lat = sorted(s["latencies"])
            self._store.log("sig_latency", {
                "endpoint": key, "category": s["category"], "count": len(lat),
                "p50_s": round(_percentile(lat, 0.5), 3), "p95_s": round(_percentile(lat, 0.95), 3),
                "max_s": round(lat[-1], 3), "timeouts": s["timeouts"], "errors": s["errors"],
            })  # fmt: skip
        self._stats = {}
