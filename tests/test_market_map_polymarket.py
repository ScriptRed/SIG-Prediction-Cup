"""Polymarket side of the market_map matcher (predcup.market_map,
Polymarket section). Same traps as Kalshi: 2026 general election only,
party read from the market question (never the slug or the candidate
label), and anything ambiguous is no match. Shapes copied from live Gamma
public-search results, 2026-10-01."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from predcup.market_map import match_poly_party, poly_search_query

FIXTURE = Path(__file__).parent / "fixtures/poly_search_2026-10-01.json"
_n = iter(range(10_000, 99_999))


def mkt(question: str, *, active=True, closed=False, end="2026-11-04T04:59:00Z", outcomes='["Yes", "No"]',
        slug=None, group="") -> dict:  # fmt: skip
    i = next(_n)
    return {
        "id": str(i), "question": question, "conditionId": f"0x{i}",
        "slug": slug if slug is not None else question.lower().replace(" ", "-").strip("?"),
        "outcomes": outcomes, "outcomePrices": '["0.4", "0.6"]',
        "clobTokenIds": json.dumps([f"{i}1", f"{i}0"]),
        "active": active, "closed": closed, "acceptingOrders": active and not closed, "endDate": end,
        "groupItemTitle": group,
    }  # fmt: skip


def ev(title: str, *markets: dict, closed=False) -> dict:
    return {"id": title, "title": title, "slug": title.lower().replace(" ", "-"), "closed": closed, "markets": list(markets)}


TX_WINNER = ev(
    "Texas Senate Election Winner",
    mkt("Will the Democrats win the Texas Senate race in 2026?", group="James Talarico (D)"),
    mkt("Will the Republicans win the Texas Senate race in 2026?", group="Ken Paxton (R)"),
    mkt("Will Person A win the Texas Senate race in 2026?", active=False),
)
TX_TRAPS = [
    ev("Texas Senate Election Margin of Victory",
       mkt("Will the Republican Party candidate win the 2026 Texas Senate election by 12% or more?"),
       mkt("Will the Democratic Party candidate win the 2026 Texas Senate election by 0%-3%?")),
    ev("Texas Senate Election: Tarrant County Winner",
       mkt("Will the Democrats win Tarrant County in the Texas Senate race in 2026?"),
       mkt("Will the Republicans win Tarrant County in the Texas Senate race in 2026?")),
    ev("Which party will win the Texas State Senate in 2026?",
       mkt("Will the Democratic Party control the Texas Senate after the 2026 Midterm elections?"),
       mkt("Will the Republican Party control the Texas Senate after the 2026 Midterm elections?")),
    ev("Which Senate races will be within 5%?", mkt("Will the Texas 2026 Senate race be within 5%?")),
    ev("Jasmine Crockett Texas Senate Race Result",
       mkt("Will Jasmine Crockett win the 2026 Texas Senate Democratic Primary and General Elections?", closed=True),
       closed=True),
    ev("Which party will win the Senate in 2026?",
       mkt("Will the Democratic Party control the Senate after the 2026 Midterm elections?"),
       mkt("Will the Republican Party control the Senate after the 2026 Midterm elections?")),
]  # fmt: skip


def tx(party: str, events=None):
    return match_poly_party(events if events is not None else [*TX_TRAPS, TX_WINNER], "Senate", "TX", "", party)


def q_of(hit, events) -> str:
    for e in events:
        for m in e["markets"]:
            if hit is not None and json.loads(m["clobTokenIds"])[0] == hit.ref:
                return m["question"]
    raise AssertionError("no such token")


# --- the right market among the traps ------------------------------------------


@pytest.mark.parametrize("party,word", [("R", "Republicans"), ("D", "Democrats")])
def test_state_senate_winner_market_is_found_among_traps(party, word):
    hit, note = tx(party)
    assert hit is not None, note
    assert q_of(hit, [TX_WINNER]) == f"Will the {word} win the Texas Senate race in 2026?"


def test_ref_is_the_yes_token():
    hit, _ = tx("R")
    m = TX_WINNER["markets"][1]
    assert hit.ref == json.loads(m["clobTokenIds"])[0]


def test_only_traps_means_no_match():
    hit, note = tx("R", TX_TRAPS)
    assert hit is None and "no Polymarket" in note


def test_governor_race():
    events = [ev("Nevada Governor Election Winner",
                 mkt("Will the Democrats win the Nevada governor race in 2026?"),
                 mkt("Will the Republicans win the Nevada governor race in 2026?")),
              ev("Nevada Governor Election: Clark County Winner",
                 mkt("Will the Republicans win Clark County in the Nevada governor race in 2026?")),
              ev("Nevada Governor Election: Turnout",
                 mkt("Will total votes cast in the 2026 midterm election for Governor of Nevada be less than 900,000?"))]  # fmt: skip
    hit, _ = match_poly_party(events, "Governor", "NV", "", "R")
    assert q_of(hit, events) == "Will the Republicans win the Nevada governor race in 2026?"


def test_office_must_match():
    # a Senate question never satisfies a Governor race
    hit, _ = match_poly_party([TX_WINNER], "Governor", "TX", "", "R")
    assert hit is None


def test_state_must_match_exactly():
    # "West Virginia" contains "Virginia"; the question must name the race's state
    events = [ev("West Virginia Senate Election Winner",
                 mkt("Will the Republicans win the West Virginia Senate race in 2026?"))]  # fmt: skip
    assert match_poly_party(events, "Senate", "VA", "", "R")[0] is None
    assert match_poly_party(events, "Senate", "WV", "", "R")[0] is not None


def test_house_district_seat():
    events = [ev("PA-07 House Election Winner",
                 mkt("Will the Republican Party win the PA-07 House seat?", group="Ryan Mackenzie (R)"),
                 mkt("Will the Democratic Party win the PA-07 House seat?", group="Bob Brooks (D)")),
              ev("PA-07 House Election Margin of Victory",
                 mkt("Will the Democratic Party candidate win the 2026 PA-07 House election by 0%-3%?"))]  # fmt: skip
    hit, _ = match_poly_party(events, "House", "PA", "07", "D")
    assert q_of(hit, events) == "Will the Democratic Party win the PA-07 House seat?"
    assert match_poly_party(events, "House", "PA", "7", "D")[0] == hit  # district padded


def test_chamber_control():
    hit, _ = match_poly_party(TX_TRAPS, "Senate", "US", "", "R")
    assert q_of(hit, TX_TRAPS) == "Will the Republican Party control the Senate after the 2026 Midterm elections?"
    events = [ev("Which party will win the House in 2026?",
                 mkt("Will the Democratic Party control the House after the 2026 Midterm elections?"))]  # fmt: skip
    assert match_poly_party(events, "House", "US", "", "D")[0] is not None


def test_independent_only_at_party_level():
    events = [ev("Michigan Governor Election Winner",
                 mkt("Will an independent win the Michigan governor race in 2026?"),
                 mkt("Will Mike Duggan win the Michigan governor race in 2026?"))]  # fmt: skip
    hit, _ = match_poly_party(events, "Governor", "MI", "", "I")
    assert q_of(hit, events) == "Will an independent win the Michigan governor race in 2026?"
    margin = [ev("Nebraska Senate Election Margin of Victory",
                 mkt("Will an Independent Candidate win the 2026 Nebraska Senate election by 0%-3%?"))]  # fmt: skip
    assert match_poly_party(margin, "Senate", "NE", "", "I")[0] is None


# --- 2026 general only -------------------------------------------------------------


def test_other_cycle_is_not_matched():
    events = [ev("Texas Senate Election Winner", mkt("Will the Republicans win the Texas Senate race in 2028?", end="2028-11-08T00:00:00Z"))]
    assert match_poly_party(events, "Senate", "TX", "", "R")[0] is None


def test_house_question_without_year_needs_a_2026_end_date():
    old = [ev("NE-02 House Election Winner",
              mkt("Will the Republican Party win the NE-02 House seat?", end="2024-11-06T00:00:00Z"))]  # fmt: skip
    assert match_poly_party(old, "House", "NE", "02", "R")[0] is None
    no_date = [ev("NE-02 House Election Winner", mkt("Will the Republican Party win the NE-02 House seat?", end=None))]
    assert match_poly_party(no_date, "House", "NE", "02", "R")[0] is None


@pytest.mark.parametrize("title", ["Texas Senate Republican Primary Winner", "Texas Senate Runoff", "Texas Senate Nominee"])
def test_primary_runoff_nominee_events_are_skipped_even_with_matching_question(title):
    events = [ev(title, mkt("Will the Republicans win the Texas Senate race in 2026?"))]
    assert match_poly_party(events, "Senate", "TX", "", "R")[0] is None


@pytest.mark.parametrize("over", [{"closed": True}, {"active": False}])
def test_closed_or_inactive_markets_are_skipped(over):
    events = [ev("Texas Senate Election Winner", mkt("Will the Republicans win the Texas Senate race in 2026?", **over))]
    assert match_poly_party(events, "Senate", "TX", "", "R")[0] is None


# --- party from the question, not the slug ---------------------------------------


def test_party_comes_from_question_not_slug_or_label():
    m = mkt("Will the Democrats win the Texas Senate race in 2026?",
            slug="will-the-republicans-win-the-texas-senate-race-in-2026", group="Ken Paxton (R)")  # fmt: skip
    events = [ev("Texas Senate Election Winner", m)]
    assert match_poly_party(events, "Senate", "TX", "", "R")[0] is None
    assert match_poly_party(events, "Senate", "TX", "", "D")[0] is not None


# --- ambiguity --------------------------------------------------------------------


def test_two_qualifying_markets_is_ambiguous():
    dup = ev("Texas Senate Winner (2)", mkt("Will the Republicans win the Texas Senate race in 2026?"))
    hit, note = tx("R", [TX_WINNER, dup])
    assert hit is None and "ambiguous" in note


def test_same_market_in_two_results_is_not_ambiguous():
    hit, _ = tx("R", [TX_WINNER, TX_WINNER])
    assert hit is not None


def test_non_yes_no_market_is_skipped():
    events = [ev("Texas Senate Election Winner",
                 mkt("Will the Republicans win the Texas Senate race in 2026?", outcomes='["No", "Yes"]'))]  # fmt: skip
    assert match_poly_party(events, "Senate", "TX", "", "R")[0] is None


def test_unknown_party_is_no_match():
    assert tx("L")[0] is None


# --- queries ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "office,state,district,query",
    [("Senate", "TX", "", "Texas Senate Election Winner"), ("Governor", "NV", "", "Nevada Governor Election Winner"),
     ("House", "PA", "7", "PA-07 House Election Winner"), ("Senate", "US", "", "Which party will win the Senate in 2026?"),
     ("House", "US", "", "Which party will win the House in 2026?")],
)  # fmt: skip
def test_search_query(office, state, district, query):
    assert poly_search_query(office, state, district) == query


# --- live search results ------------------------------------------------------------------


@pytest.mark.skipif(not FIXTURE.exists(), reason="live fixture not captured")
@pytest.mark.parametrize(
    "race,party,question",
    [(("Senate", "TX", ""), "R", "Will the Republicans win the Texas Senate race in 2026?"),
     (("Senate", "TX", ""), "D", "Will the Democrats win the Texas Senate race in 2026?"),
     (("Governor", "NV", ""), "R", "Will the Republicans win the Nevada governor race in 2026?"),
     (("House", "PA", "07"), "D", "Will the Democratic Party win the PA-07 House seat?"),
     (("Senate", "US", ""), "R", "Will the Republican Party control the Senate after the 2026 Midterm elections?")],
)  # fmt: skip
def test_live_search_results(race, party, question):
    events = json.loads(FIXTURE.read_text())[poly_search_query(*race)]
    hit, note = match_poly_party(events, *race, party)
    assert hit is not None, note
    assert hit.text == question


# --- writing the column into market_map.csv rows ------------------------------------

from predcup.market_map import ExternalMarket, apply_poly_column  # noqa: E402

VERIFIED = {"platform_id": "294", "kalshi_ticker": "SENATETX-26-R", "poly_token_id": "OLD_MARGIN",
            "polarity": "same", "rule_diff_notes": "Kalshi YES label 'Ken Paxton'; Polymarket event texas-senate-margin",
            "confidence": "0.8", "verified": "true", "tier": "A", "fusion_risk": "false"}  # fmt: skip
UNVERIFIED = {**VERIFIED, "platform_id": "388", "kalshi_ticker": "SENATERI-26-R", "verified": "false", "tier": "",
              "rule_diff_notes": "Kalshi YES label 'X'; no Polymarket event found for query 'x'"}  # fmt: skip


def test_verified_row_changes_only_in_the_polymarket_column_and_is_reported():
    rows, review = apply_poly_column([VERIFIED], {"294": (ExternalMarket("NEW", "Will the Republicans win..."), "")})
    assert rows[0] == {**VERIFIED, "poly_token_id": "NEW"}
    assert review == [{"platform_id": "294", "kalshi_ticker": "SENATETX-26-R", "old_poly_token_id": "OLD_MARGIN",
                       "new_poly_token_id": "NEW", "new_question": "Will the Republicans win...", "note": ""}]  # fmt: skip


def test_verified_row_with_no_match_loses_its_old_id_but_keeps_everything_else():
    rows, review = apply_poly_column([VERIFIED], {"294": (None, "no Polymarket 2026 general market")})
    assert rows[0] == {**VERIFIED, "poly_token_id": ""}
    assert review[0]["new_poly_token_id"] == "" and "no Polymarket" in review[0]["note"]


def test_unverified_row_gets_id_and_fresh_polymarket_note_confidence_untouched():
    rows, review = apply_poly_column([UNVERIFIED], {"388": (None, "Polymarket ambiguous: 2 markets")})
    assert rows[0]["poly_token_id"] == ""
    assert rows[0]["rule_diff_notes"] == "Kalshi YES label 'X'; Polymarket ambiguous: 2 markets"
    assert rows[0]["confidence"] == UNVERIFIED["confidence"] and rows[0]["verified"] == "false"
    assert review == []
    rows, _ = apply_poly_column([UNVERIFIED], {"388": (ExternalMarket("T", "q"), "")})
    assert rows[0]["poly_token_id"] == "T" and rows[0]["rule_diff_notes"] == "Kalshi YES label 'X'"


def test_rows_without_a_result_are_untouched():
    rows, review = apply_poly_column([VERIFIED, UNVERIFIED], {})
    assert rows == [VERIFIED, UNVERIFIED] and review == []
