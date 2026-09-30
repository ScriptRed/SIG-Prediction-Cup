"""Senate matching against real Kalshi /events payloads (trimmed), fetched
2026-09-30. Kalshi lists 2026 Senate general elections as series
SENATE<ST>, event SENATE<ST>-26, markets -D / -R whose YES label is the
candidate's name but whose rules resolve on party. The ticker's state code
is not trustworthy (SENATELA-26 is the Kentucky race), so the state comes
from the event title."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from predcup.market_map import (
    index_senate_events,
    is_2026_general_event,
    match_senate_party,
)

FIXTURE = Path(__file__).parent / "fixtures" / "kalshi_senate_events_2026-09-30.json"


@pytest.fixture(scope="module")
def events() -> list[dict]:
    by_series = json.loads(FIXTURE.read_text())
    return [e for evs in by_series.values() for e in evs]


@pytest.fixture(scope="module")
def index(events):
    return index_senate_events(events)


def test_2028_and_2024_events_are_not_2026_generals(events):
    by_ticker = {e["event_ticker"]: e for e in events}
    assert is_2026_general_event(by_ticker["SENATEAK-26"])
    assert not is_2026_general_event(by_ticker["SENATEAK-28"])  # party-named, same series
    assert not is_2026_general_event(by_ticker["SENATEFL-24"])


def test_primary_event_is_rejected():
    ev = {
        "event_ticker": "KXSENATETXR-26",
        "title": "Texas Republican Senate nominee",
        "sub_title": "In 2026",
        "markets": [{"ticker": "KXSENATETXR-26-KPAX", "status": "active", "title": "Will Ken Paxton be the nominee?"}],
    }
    assert not is_2026_general_event(ev)


def test_state_comes_from_event_title_not_ticker(index):
    assert index["KY"][0]["event_ticker"] == "SENATELA-26"  # titled "Kentucky Senate winner?"
    assert index["LA"][0]["event_ticker"] == "KXSENATELA-26NOV"


def test_special_election_series_is_found(index):
    assert [e["event_ticker"] for e in index["FL"]] == ["SENATEFLS-26"]


def test_party_named_2028_event_does_not_shadow_2026(index):
    assert [e["event_ticker"] for e in index["AK"]] == ["SENATEAK-26"]


def test_empty_party_series_is_ignored(index):
    assert [e["event_ticker"] for e in index["MA"]] == ["SENATEMA-26"]


def test_match_democrat_and_republican_by_ticker_suffix_and_title(index):
    ma = index["MA"][0]["markets"]
    assert match_senate_party(ma, "D").ref == "SENATEMA-26-D"  # YES label "Ed Markey"
    assert match_senate_party(ma, "R").ref == "SENATEMA-26-R"


def test_independent_matches_the_single_independent_candidate(index):
    assert match_senate_party(index["NE"][0]["markets"], "I").ref == "SENATENE-26-DOSB"
    assert match_senate_party(index["MT"][0]["markets"], "I").ref == "SENATEMT-26-IND"


def test_no_independent_market_means_no_match(index):
    assert match_senate_party(index["MA"][0]["markets"], "I") is None


def test_suffix_and_title_must_agree():
    markets = [{"ticker": "SENATEXX-26-D", "status": "active", "title": "Will Republicans win the Senate race in X?", "subtitle": ""}]
    assert match_senate_party(markets, "D") is None
    assert match_senate_party(markets, "R") is None


def test_two_independents_is_ambiguous():
    markets = [
        {"ticker": "SENATEXX-26-IA", "status": "active", "title": "Will A (as an independent) win?", "subtitle": "A:: Independent"},
        {"ticker": "SENATEXX-26-IB", "status": "active", "title": "Will B (as an independent) win?", "subtitle": "B:: Independent"},
    ]
    assert match_senate_party(markets, "I") is None


def test_inactive_market_is_not_matched():
    markets = [{"ticker": "SENATEXX-26-D", "status": "closed", "title": "Will Democratics win the Senate race in X?", "subtitle": ""}]
    assert match_senate_party(markets, "D") is None
