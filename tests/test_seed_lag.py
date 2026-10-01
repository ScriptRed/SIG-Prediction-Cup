"""predcup/seed_lag.py (logger + lag estimate) and scripts/seed_lag_report.py."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from predcup.mapped import MappedMarket
from predcup.seed_lag import (
    LagSample,
    SeedLagLogger,
    SeedLagStore,
    estimate_move_lags,
    lagged_correlation,
    register_seed_lag_logger,
    summarize_lags,
)
from predcup.store import EventStore
from predcup.venues.kalshi import parse_market

TID = "550e8400-e29b-41d4-a716-446655440000"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def km(ticker, bid, ask):
    return parse_market({"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0],
                         "yes_bid_dollars": bid, "yes_ask_dollars": ask})  # fmt: skip


def mm(ex, ticker, polarity="same"):
    return MappedMarket(market_id=f"m{ex}", exchange_id=ex, race_key=f"R{ex}", party="D", title="t",
                        kalshi_ticker=ticker, polarity=polarity, fusion_risk=False)  # fmt: skip


class Venue:
    def __init__(self, tops):
        self.tops = tops

    async def get_top_of_books(self, ids, tournament_id):
        assert tournament_id == TID
        return {i: self.tops[i] for i in ids if i in self.tops}

    def __getattr__(self, name):
        raise AssertionError(f"seed-lag logger must not call venue.{name}")


class Kalshi:
    def __init__(self, markets=None, error=None):
        self.markets, self.error = markets or {}, error

    async def get_markets(self, tickers):
        if self.error:
            raise self.error
        return {t: self.markets[t] for t in tickers if t in self.markets}


# --- logger -------------------------------------------------------------------------


def test_logger_records_sig_and_polarity_adjusted_kalshi_mids(tmp_path):
    store = SeedLagStore(tmp_path / "db.sqlite")
    logger = SeedLagLogger(
        venue=Venue({"1": (0.60, 0.64), "2": (0.40, None)}),
        kalshi=Kalshi({"A-26-D": km("A-26-D", "0.61", "0.63"), "B-26-R": km("B-26-R", "0.55", "0.57")}),
        store=store, event_store=EventStore(":memory:"), tournament_id=TID,
        markets=[mm("1", "A-26-D"), mm("2", "B-26-R", "inverted")], clock=lambda: T0,
    )  # fmt: skip
    asyncio.run(logger.run_once())
    rows = {r.exchange_id: r for r in store.samples()}
    assert rows["1"].sig_mid == pytest.approx(0.62) and rows["1"].kalshi_mid == pytest.approx(0.62)
    assert rows["2"].sig_mid is None and rows["2"].sig_bid == 0.40  # one-sided SIG book: no mid
    assert rows["2"].kalshi_mid == pytest.approx(0.44)  # 1 - 0.56
    assert rows["2"].kalshi_bid == 0.55 and rows["2"].polarity == "inverted"  # raw Kalshi quotes kept
    assert rows["1"].ts == T0


def test_logger_read_failure_is_logged_and_writes_nothing(tmp_path):
    store, events = SeedLagStore(tmp_path / "db.sqlite"), EventStore(":memory:")
    logger = SeedLagLogger(venue=Venue({}), kalshi=Kalshi(error=RuntimeError("down")), store=store,
                           event_store=events, tournament_id=TID, markets=[mm("1", "A-26-D")], clock=lambda: T0)  # fmt: skip
    asyncio.run(logger.run_once())
    assert store.samples() == []
    assert "down" in events.all_events("seed_lag_failed")[0]["payload"]["error"]


def test_store_window_filter(tmp_path):
    store = SeedLagStore(tmp_path / "db.sqlite")
    store.insert([LagSample(T0 + timedelta(minutes=i), "1", "m1", "R1", "D", "A-26-D", "same",
                            0.5, 0.52, 0.51, 0.5, 0.52, 0.51) for i in range(5)])  # fmt: skip
    got = store.samples(since=T0 + timedelta(minutes=1), until=T0 + timedelta(minutes=3))
    assert [s.ts.minute for s in got] == [1, 2, 3]


# --- lag estimate -------------------------------------------------------------------


def series(ex, k_mids, s_mids, step_s=30):
    return [
        LagSample(T0 + timedelta(seconds=i * step_s), ex, "m", "R", "D", "K", "same", None, None, s, None, None, k)
        for i, (k, s) in enumerate(zip(k_mids, s_mids))
    ]


def test_move_followed_after_three_samples():
    k = [0.50, 0.50, 0.53, 0.53, 0.53, 0.53, 0.53, 0.53]
    s = [0.49, 0.49, 0.49, 0.49, 0.50, 0.515, 0.52, 0.52]  # 50% of +3 pts reached at sample 5
    (move,) = estimate_move_lags(series("1", k, s), min_move=0.02, follow_fraction=0.5, horizon_seconds=600)
    assert move.kalshi_move == pytest.approx(0.03)
    assert move.lag_seconds == 90  # samples 2 -> 5
    assert move.followed


def test_down_moves_unfollowed_moves_and_small_moves():
    k = [0.60, 0.57, 0.57, 0.57, 0.575, 0.575]  # -3 pts, then a +0.5 pt wiggle (ignored)
    s = [0.60, 0.60, 0.60, 0.60, 0.60, 0.60]  # SIG never follows
    (move,) = estimate_move_lags(series("1", k, s), min_move=0.02, follow_fraction=0.5, horizon_seconds=600)
    assert not move.followed and move.lag_seconds is None


def test_horizon_censors_late_follow():
    k = [0.50, 0.53] + [0.53] * 30
    s = [0.50] * 25 + [0.53] * 7  # follows only after 23 samples = 690 s
    (move,) = estimate_move_lags(series("1", k, s), min_move=0.02, follow_fraction=0.5, horizon_seconds=600)
    assert not move.followed


def test_gaps_in_sampling_are_not_treated_as_a_move():
    rows = series("1", [0.50, 0.50], [0.50, 0.50]) + [
        LagSample(T0 + timedelta(hours=2), "1", "m", "R", "D", "K", "same", None, None, 0.6, None, None, 0.6)
    ]
    assert estimate_move_lags(rows, min_move=0.02, follow_fraction=0.5, horizon_seconds=600, max_gap_seconds=90) == []


def test_markets_are_kept_separate_and_summary():
    a = series("1", [0.50, 0.53, 0.53, 0.53], [0.50, 0.50, 0.52, 0.52])  # lag 30
    b = series("2", [0.50, 0.53, 0.53, 0.53], [0.50, 0.50, 0.50, 0.52])  # lag 60
    c = series("3", [0.50, 0.53, 0.53, 0.53], [0.50, 0.50, 0.50, 0.50])  # not followed
    moves = estimate_move_lags(a + b + c, min_move=0.02, follow_fraction=0.5, horizon_seconds=600)
    assert sorted((m.exchange_id, m.lag_seconds) for m in moves) == [("1", 30), ("2", 60), ("3", None)]
    summ = summarize_lags(moves)
    assert summ.moves == 3 and summ.followed == 2
    assert summ.median_seconds == 45 and summ.max_seconds == 60


def test_lagged_correlation_peaks_at_the_true_lag():
    import random

    rng = random.Random(1)
    k = [0.5]
    for _ in range(300):
        k.append(min(0.95, max(0.05, k[-1] + rng.choice([-0.01, 0, 0, 0.01]))))
    s = [0.5] * 4 + k[:-4]  # SIG copies Kalshi 4 samples later
    corr = lagged_correlation(series("1", k, s), max_lag_samples=10)
    assert max(corr, key=corr.get) == 4


# --- registration and config ---------------------------------------------------------


class FakeApp:
    def __init__(self, db):
        self.venue, self.kalshi, self.store, self.alerter, self.tid = Venue({}), Kalshi(), EventStore(":memory:"), None, TID
        self.tasks, self.periodic = [], []

    def add_task(self, f):
        self.tasks.append(f)

    async def _periodic(self, name, interval, step):
        self.periodic.append((name, interval))


CUP = [{"id": "1", "exchange_id": "11", "title": "t", "party": "D", "race_key": "A"}]
MAP = [{"platform_id": "1", "kalshi_ticker": "A-26-D", "polarity": "same", "verified": "true", "fusion_risk": "false"}]


def test_register_off_by_default_and_on_when_enabled(tmp_path):
    app = FakeApp(tmp_path)
    settings = {"storage": {"db_path": str(tmp_path / "x.db")},
                "seed_lag": {"enabled": False, "interval_seconds": 30, "verified_only": True}}  # fmt: skip
    assert register_seed_lag_logger(app, settings, CUP, MAP) is None and app.tasks == []
    settings["seed_lag"]["enabled"] = True
    assert register_seed_lag_logger(app, settings, CUP, MAP) is not None
    asyncio.run(app.tasks[0]())
    assert app.periodic == [("seed_lag", 30.0)]


def test_settings_yaml_ships_with_seed_lag_disabled():
    import yaml

    s = yaml.safe_load(Path("config/settings.yaml").read_text())
    assert s["seed_lag"]["enabled"] is False and s["seed_lag"]["interval_seconds"] == 30


def test_module_never_touches_orders():
    src = Path("predcup/seed_lag.py").read_text()
    for forbidden in ("place_order", "place_batch", "cancel", "OrderRouter", "risk.check"):
        assert forbidden not in src


# --- report script -------------------------------------------------------------------


def test_report_script_prints_per_market_and_overall(tmp_path):
    from scripts import seed_lag_report

    db = tmp_path / "db.sqlite"
    store = SeedLagStore(db)
    store.insert(series("1", [0.50, 0.53, 0.53, 0.53], [0.50, 0.50, 0.52, 0.52])
                 + series("2", [0.50, 0.53, 0.53, 0.53], [0.50, 0.50, 0.50, 0.50]))  # fmt: skip
    lines: list[str] = []
    code = seed_lag_report.main(["--db", str(db), "--since", T0.isoformat(), "--min-move", "0.02"], out=lines.append)
    text = "\n".join(lines)
    assert code == 0
    assert "Kalshi moves >= 2.0 pts: 2, followed 1 (50%)" in text
    assert "median 30 s" in text
    assert any(ln.split()[:1] == ["1"] for ln in lines) and any(ln.split()[:1] == ["2"] for ln in lines)


def test_report_script_empty_window(tmp_path):
    from scripts import seed_lag_report

    SeedLagStore(tmp_path / "db.sqlite")
    lines: list[str] = []
    assert seed_lag_report.main(["--db", str(tmp_path / "db.sqlite"), "--hours", "1"], out=lines.append) == 0
    assert "no seed-lag samples" in "\n".join(lines)
