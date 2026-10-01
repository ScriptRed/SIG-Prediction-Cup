"""How long do SIG's seeded quotes take to follow a Kalshi move?

    python -m scripts.seed_lag_report                  # last 24 h
    python -m scripts.seed_lag_report --hours 6 --min-move 0.02
    python -m scripts.seed_lag_report --since 2026-10-01T00:00:00+00:00 [--until ...]

Reads the seed_lag table (predcup/seed_lag.py; enable seed_lag in
config/settings.yaml) from the bot's SQLite file. Two estimates:
- move lags: for every Kalshi mid move >= --min-move between consecutive
  samples, the time until the SIG mid covers --fraction of it (resolution =
  the sampling interval, 30 s). Per market and overall.
- lagged correlation: Kalshi mid changes vs SIG mid changes L samples
  later, pooled; the peak L is a model-free estimate.
Offline and read-only: no venue is contacted.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from predcup.seed_lag import SeedLagStore, estimate_move_lags, lagged_correlation, summarize_lags

SETTINGS_PATH = Path("config/settings.yaml")


def _secs(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f} s"


def _aware(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rows_by_market(samples) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for x in samples:
        out[x.exchange_id].append(x)
    return out


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print,
         settings_path: Path = SETTINGS_PATH) -> int:  # fmt: skip
    settings = yaml.safe_load(settings_path.read_text())
    rep = settings["seed_lag"]["report"]
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=settings["storage"]["db_path"])
    p.add_argument("--hours", type=float, default=None, help="window ending now (default 24 unless --since)")
    p.add_argument("--since", type=_aware, default=None, help="ISO time, UTC if no offset")
    p.add_argument("--until", type=_aware, default=None)
    p.add_argument("--min-move", type=float, default=float(rep["min_move"]))
    p.add_argument("--fraction", type=float, default=float(rep["follow_fraction"]))
    p.add_argument("--horizon", type=float, default=float(rep["horizon_seconds"]), help="seconds")
    p.add_argument("--max-gap", type=float, default=float(rep["max_gap_seconds"]), help="seconds")
    p.add_argument("--max-lag-samples", type=int, default=int(rep["max_lag_samples"]))
    a = p.parse_args(argv)

    since = a.since
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(hours=a.hours if a.hours is not None else 24)
    store = SeedLagStore(a.db)
    try:
        samples = store.samples(since=since, until=a.until)
    finally:
        store.close()
    if not samples:
        out(f"no seed-lag samples in {a.db} since {since.isoformat()} (is seed_lag.enabled on?)")
        return 0

    moves = estimate_move_lags(samples, min_move=a.min_move, follow_fraction=a.fraction,
                               horizon_seconds=a.horizon, max_gap_seconds=a.max_gap)  # fmt: skip
    n_markets = len({s.exchange_id for s in samples})
    out(f"{len(samples)} samples, {n_markets} markets, {samples[0].ts:%Y-%m-%d %H:%M} -> {samples[-1].ts:%Y-%m-%d %H:%M} UTC")
    out(f"follow = SIG mid covers {a.fraction:.0%} of the Kalshi move within {a.horizon:.0f} s")
    out("")
    by_market = defaultdict(list)
    for m in moves:
        by_market[m.exchange_id].append(m)
    meta = {s.exchange_id: (s.race_key, s.party, s.kalshi_ticker) for s in samples}
    out(f"{'ex id':<6} {'race':<16} {'pty':<3} {'kalshi':<20} {'moves':>5} {'followed':>8} {'median':>8} {'max':>8}")
    for ex in sorted(by_market, key=lambda e: meta[e]):
        s = summarize_lags(by_market[ex])
        race, party, ticker = meta[ex]
        out(f"{ex:<6} {race:<16} {party:<3} {ticker:<20} {s.moves:>5} {s.followed:>8} "
            f"{_secs(s.median_seconds):>8} {_secs(s.max_seconds):>8}")  # fmt: skip
    quiet = n_markets - len(by_market)
    if quiet:
        out(f"({quiet} market(s) had no Kalshi move >= {a.min_move * 100:.1f} pts)")

    s = summarize_lags(moves)
    out("")
    pct = f" ({s.followed / s.moves:.0%})" if s.moves else ""
    out(f"Kalshi moves >= {a.min_move * 100:.1f} pts: {s.moves}, followed {s.followed}{pct}")
    if s.followed:
        out(f"lag: median {_secs(s.median_seconds)}, p75 {_secs(s.p75_seconds)}, max {_secs(s.max_seconds)}")

    corr = lagged_correlation(samples, max_lag_samples=a.max_lag_samples, max_gap_seconds=a.max_gap)
    if corr:
        best = max(corr, key=corr.get)
        out("lagged correlation (Kalshi change vs SIG change L samples later): "
            + ", ".join(f"L{L} {c:+.2f}" for L, c in sorted(corr.items())))  # fmt: skip
        steps = sorted((b.ts - a.ts).total_seconds() for rows in _rows_by_market(samples).values()
                       for a, b in zip(rows, rows[1:]))  # fmt: skip
        approx = f" (~{best * steps[len(steps) // 2]:.0f} s at the median sample spacing)" if steps else ""
        out(f"peak at L={best} samples{approx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
