"""Seed-lag logger: how long SIG's seeded quotes take to follow Kalshi.

Every `interval_seconds` (30), read the SIG top of book and the Kalshi quote
for each mapped market and write one row per market to the `seed_lag`
table: SIG bid/ask/mid and Kalshi bid/ask (raw YES) plus the Kalshi mid in
SIG YES terms (polarity applied). Read-only; a failed read is logged to
events_log as `seed_lag_failed` and skipped. Off by default (settings
seed_lag.enabled). scripts/seed_lag_report.py turns the table into a lag
estimate with the pure functions below.

The table lives in the bot's SQLite file (WAL) on its own connection, so
the report script can read it while the bot runs.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from predcup.mapped import MappedMarket, load_mapped_markets, read_both
from predcup.mapping_review import kalshi_mid_in_sig_terms
from predcup.store import EventStore

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS seed_lag (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        exchange_id TEXT NOT NULL,
        market_id TEXT NOT NULL,
        race_key TEXT NOT NULL,
        party TEXT NOT NULL,
        kalshi_ticker TEXT NOT NULL,
        polarity TEXT NOT NULL,
        sig_bid REAL,
        sig_ask REAL,
        sig_mid REAL,
        kalshi_bid REAL,
        kalshi_ask REAL,
        kalshi_mid REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS seed_lag_by_ts ON seed_lag (ts)",
)

_COLUMNS = ("ts", "exchange_id", "market_id", "race_key", "party", "kalshi_ticker", "polarity",
            "sig_bid", "sig_ask", "sig_mid", "kalshi_bid", "kalshi_ask", "kalshi_mid")  # fmt: skip


@dataclass(frozen=True)
class LagSample:
    ts: datetime
    exchange_id: str
    market_id: str
    race_key: str
    party: str
    kalshi_ticker: str
    polarity: str
    sig_bid: float | None
    sig_ask: float | None
    sig_mid: float | None
    kalshi_bid: float | None  # raw Kalshi YES
    kalshi_ask: float | None
    kalshi_mid: float | None  # in SIG YES terms


class SeedLagStore:
    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        for statement in _SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    def insert(self, samples: list[LagSample]) -> None:
        rows = [(s.ts.astimezone(timezone.utc).isoformat(), *(getattr(s, c) for c in _COLUMNS[1:])) for s in samples]
        with self._conn:
            self._conn.executemany(
                f"INSERT INTO seed_lag ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)})", rows
            )

    def samples(self, since: datetime | None = None, until: datetime | None = None) -> list[LagSample]:
        where, params = [], []
        if since is not None:
            where.append("ts >= ?")
            params.append(since.astimezone(timezone.utc).isoformat())
        if until is not None:
            where.append("ts <= ?")
            params.append(until.astimezone(timezone.utc).isoformat())
        sql = f"SELECT {', '.join(_COLUMNS)} FROM seed_lag"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self._conn.execute(sql + " ORDER BY ts, id", params).fetchall()
        return [LagSample(datetime.fromisoformat(r[0]), *r[1:]) for r in rows]

    def close(self) -> None:
        self._conn.close()


def _mid(bid: float | None, ask: float | None) -> float | None:
    return None if bid is None or ask is None else (bid + ask) / 2


class SeedLagLogger:
    def __init__(
        self,
        *,
        venue,
        kalshi,
        store: SeedLagStore,
        event_store: EventStore,
        tournament_id: str,
        markets: list[MappedMarket],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.venue = venue
        self.kalshi = kalshi
        self.store = store
        self.event_store = event_store
        self.tid = tournament_id
        self.markets = markets
        self.clock = clock

    async def run_once(self) -> int:
        if not self.markets:
            return 0
        try:
            sig, kalshi = await read_both(self.venue, self.kalshi, self.tid, self.markets)
        except Exception as e:
            self.event_store.log("seed_lag_failed", {"error": repr(e)[:300]})
            return 0
        now = self.clock()
        rows = []
        for m in self.markets:
            bid, ask = sig.get(m.exchange_id, (None, None))
            k = kalshi.get(m.kalshi_ticker)
            rows.append(LagSample(
                ts=now, exchange_id=m.exchange_id, market_id=m.market_id, race_key=m.race_key, party=m.party,
                kalshi_ticker=m.kalshi_ticker, polarity=m.polarity, sig_bid=bid, sig_ask=ask, sig_mid=_mid(bid, ask),
                kalshi_bid=k.yes_bid if k else None, kalshi_ask=k.yes_ask if k else None,
                kalshi_mid=kalshi_mid_in_sig_terms(k, m.polarity) if k else None,
            ))  # fmt: skip
        self.store.insert(rows)
        return len(rows)


def register_seed_lag_logger(
    app, settings: dict, cup_rows: list[dict[str, str]], map_rows: list[dict[str, str]]
) -> SeedLagLogger | None:
    """Add the logger to `app` as a periodic task when seed_lag.enabled."""
    cfg = settings.get("seed_lag", {})
    if not cfg.get("enabled", False):
        return None
    logger = SeedLagLogger(
        venue=app.venue, kalshi=app.kalshi, store=SeedLagStore(settings["storage"]["db_path"]),
        event_store=app.store, tournament_id=app.tid,
        markets=load_mapped_markets(cup_rows, map_rows, verified_only=bool(cfg["verified_only"])),
    )  # fmt: skip
    interval = float(cfg["interval_seconds"])
    app.add_task(lambda: app._periodic("seed_lag", interval, logger.run_once))
    return logger


# --- lag estimate (pure) ---------------------------------------------------------------


@dataclass(frozen=True)
class MoveLag:
    exchange_id: str
    race_key: str
    party: str
    at: datetime  # first sample showing the new Kalshi level
    kalshi_move: float  # signed, SIG YES terms
    lag_seconds: float | None  # None: SIG did not follow within the horizon

    @property
    def followed(self) -> bool:
        return self.lag_seconds is not None


def _by_market(samples: list[LagSample]) -> dict[str, list[LagSample]]:
    out: dict[str, list[LagSample]] = defaultdict(list)
    for s in samples:
        if s.sig_mid is not None and s.kalshi_mid is not None:
            out[s.exchange_id].append(s)
    for rows in out.values():
        rows.sort(key=lambda s: s.ts)
    return out


def estimate_move_lags(
    samples: list[LagSample],
    *,
    min_move: float,
    follow_fraction: float,
    horizon_seconds: float,
    max_gap_seconds: float = 90.0,
) -> list[MoveLag]:
    """A Kalshi move is a change of at least `min_move` in the Kalshi mid
    between consecutive samples (no more than `max_gap_seconds` apart, so a
    restart gap is not a move). Its lag is the time from the first sample
    showing the new Kalshi level to the first sample where the SIG mid has
    covered `follow_fraction` of the move, measured from the SIG mid just
    before it; resolution is the sampling interval. Not reached within
    `horizon_seconds`: not followed."""
    out = []
    for ex, rows in _by_market(samples).items():
        for i in range(1, len(rows)):
            prev, cur = rows[i - 1], rows[i]
            if (cur.ts - prev.ts).total_seconds() > max_gap_seconds:
                continue
            move = cur.kalshi_mid - prev.kalshi_mid  # type: ignore[operator]
            if abs(move) < min_move - 1e-9:
                continue
            lag = None
            for later in rows[i:]:
                dt = (later.ts - cur.ts).total_seconds()
                if dt > horizon_seconds:
                    break
                if (later.sig_mid - prev.sig_mid) / move >= follow_fraction - 1e-9:  # type: ignore[operator]
                    lag = dt
                    break
            out.append(MoveLag(ex, cur.race_key, cur.party, cur.ts, move, lag))
    return out


@dataclass(frozen=True)
class LagSummary:
    moves: int
    followed: int
    median_seconds: float | None
    p75_seconds: float | None
    max_seconds: float | None


def summarize_lags(moves: list[MoveLag]) -> LagSummary:
    lags = sorted(m.lag_seconds for m in moves if m.lag_seconds is not None)
    if not lags:
        return LagSummary(len(moves), 0, None, None, None)
    p75 = lags[min(len(lags) - 1, int(round(0.75 * (len(lags) - 1))))]
    return LagSummary(len(moves), len(lags), statistics.median(lags), p75, lags[-1])


def lagged_correlation(
    samples: list[LagSample], *, max_lag_samples: int, max_gap_seconds: float = 90.0
) -> dict[int, float]:
    """Correlation of Kalshi mid changes with SIG mid changes L samples
    later, pooled over markets, for L = 0..max_lag_samples. The peak is a
    second, model-free lag estimate (in samples). Changes across a sampling
    gap are dropped."""
    import numpy as np

    pairs: dict[int, tuple[list[float], list[float]]] = {L: ([], []) for L in range(max_lag_samples + 1)}
    for rows in _by_market(samples).values():
        dk, ds = [], []
        for a, b in zip(rows, rows[1:]):
            ok = (b.ts - a.ts).total_seconds() <= max_gap_seconds
            dk.append(b.kalshi_mid - a.kalshi_mid if ok else None)  # type: ignore[operator]
            ds.append(b.sig_mid - a.sig_mid if ok else None)  # type: ignore[operator]
        for L in pairs:
            for t in range(len(dk) - L):
                if dk[t] is not None and ds[t + L] is not None:
                    pairs[L][0].append(dk[t])
                    pairs[L][1].append(ds[t + L])
    out = {}
    for L, (x, y) in pairs.items():
        if len(x) < 3 or np.std(x) == 0 or np.std(y) == 0:
            continue
        out[L] = float(np.corrcoef(x, y)[0, 1])
    return out
