"""fairvalue.py v1: Kalshi mid for verified Tier A rows only. No external
match, an unverified row, a stale or one-sided Kalshi book -> no fair value
(no trading), never a guess."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from predcup.fairvalue import (
    FairValueConfig,
    FairValueTracker,
    KalshiQuote,
    kalshi_fair_value,
    load_fair_value_config,
)
from predcup.store import EventStore
from predcup.venues.kalshi import parse_market

NOW = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)
CFG = FairValueConfig(
    max_age_seconds=60,
    max_spread=0.10,
    base_uncertainty=0.005,
    thin_volume=1000,
    thin_penalty=0.01,
    stale_penalty=0.01,
    min_confidence=0.5,
)
ROW = {"platform_id": "379", "kalshi_ticker": "SENATEMA-26-D", "polarity": "same",
       "confidence": "0.8", "verified": "true", "tier": "A", "fusion_risk": "false"}  # fmt: skip


def quote(bid="0.6000", ask="0.6200", volume="50000.00", age_s=5.0) -> KalshiQuote:
    raw = {"ticker": "SENATEMA-26-D", "event_ticker": "SENATEMA-26", "title": "t", "subtitle": "",
           "yes_sub_title": "", "no_sub_title": "", "status": "active", "yes_bid_dollars": bid,
           "yes_ask_dollars": ask, "volume_fp": volume, "rules_primary": "", "rules_secondary": ""}  # fmt: skip
    return KalshiQuote(market=parse_market(raw), fetched_at=NOW - timedelta(seconds=age_s))


def fv(row=ROW, q=None, now=NOW):
    return kalshi_fair_value(row, q if q is not None else quote(), now, CFG)


def test_verified_tier_a_gets_kalshi_mid():
    r = fv()
    assert r.ok and r.value == pytest.approx(0.61)
    assert r.uncertainty == pytest.approx(0.01 + 0.005 + 0.01 * 5 / 60)  # half-spread + base + staleness


def test_inverted_polarity_uses_complement():
    assert fv(row={**ROW, "polarity": "inverted"}).value == pytest.approx(0.39)


@pytest.mark.parametrize(
    "override,reason",
    [
        ({"verified": "false"}, "not verified"),
        ({"verified": ""}, "not verified"),
        ({"tier": "B"}, "not Tier A"),
        ({"tier": ""}, "not Tier A"),
        ({"kalshi_ticker": ""}, "no Kalshi ticker"),
        ({"polarity": ""}, "polarity"),
        ({"confidence": "0.3"}, "confidence"),
    ],
)
def test_no_fair_value_without_a_trusted_mapping(override, reason):
    r = fv(row={**ROW, **override})
    assert not r.ok and r.value is None and reason in r.reason


def test_high_confidence_does_not_rescue_unverified_row():
    r = fv(row={**ROW, "verified": "false", "confidence": "1.0"})
    assert not r.ok


def test_stale_quote_gives_no_fair_value():
    r = fv(q=quote(age_s=61))
    assert not r.ok and "stale" in r.reason


def test_missing_quote_gives_no_fair_value():
    r = kalshi_fair_value(ROW, None, NOW, CFG)
    assert not r.ok and "no Kalshi quote" in r.reason


@pytest.mark.parametrize("bid,ask", [("0.0000", "0.6200"), ("0.6000", "1.0000"), ("0.0000", "1.0000")])
def test_one_sided_or_empty_kalshi_book_gives_no_fair_value(bid, ask):
    r = fv(q=quote(bid=bid, ask=ask))
    assert not r.ok and "two-sided" in r.reason


def test_wide_kalshi_spread_gives_no_fair_value():
    r = fv(q=quote(bid="0.4000", ask="0.5500"))
    assert not r.ok and "spread" in r.reason


def test_thin_book_widens_uncertainty():
    assert fv(q=quote(volume="10.00")).uncertainty == pytest.approx(fv().uncertainty + 0.01)


def test_crossed_kalshi_book_gives_no_fair_value():
    r = fv(q=quote(bid="0.6300", ask="0.6100"))
    assert not r.ok


def test_config_loads_from_settings():
    cfg = load_fair_value_config({
        "fair_value": {"max_outside_data_age_seconds": 60, "min_confidence_to_trade": 0.5,
                       "kalshi": {"max_spread": 0.1, "base_uncertainty": 0.005, "thin_volume": 1000,
                                  "thin_penalty": 0.01, "stale_penalty": 0.01}}})  # fmt: skip
    assert cfg == CFG


def test_tracker_logs_changes_only(tmp_path):
    store = EventStore(tmp_path / "e.db")
    t = FairValueTracker(store)
    t.update("1068", fv())
    t.update("1068", fv())  # unchanged -> not logged again
    t.update("1068", fv(q=quote(bid="0.6400", ask="0.6600")))
    t.update("1068", fv(q=quote(age_s=100)))  # became unavailable -> logged
    events = store.all_events("fair_value")
    assert [e["payload"]["value"] for e in events] == [pytest.approx(0.61), pytest.approx(0.65), None]
    assert events[-1]["payload"]["reason"].startswith("stale")
    assert t.current("1068").ok is False
