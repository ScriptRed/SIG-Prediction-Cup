"""Regex parsing of Cup market titles — deterministic, no LLM (CLAUDE.md
Hard Rule 2: LLM output may only produce alerts/flags, never feed directly
into mapping logic that risk/sizing relies on).
"""

import json
from pathlib import Path

import pytest

from predcup.cup_markets import ParsedTitle, build_rows, parse_title, race_key

FIXTURE = Path(__file__).parent / "fixtures" / "cup_markets_2026-09-28.json"


def test_parse_senate_republican():
    parsed = parse_title("Will the Republican Party win the Rhode Island Senate?")
    assert parsed == ParsedTitle(party="R", office="Senate", state="RI", district="")


def test_parse_senate_democratic():
    parsed = parse_title("Will the Democratic Party win the Oklahoma Senate?")
    assert parsed == ParsedTitle(party="D", office="Senate", state="OK", district="")


def test_parse_governor():
    parsed = parse_title("Will the Republican Party win the Rhode Island Governor?")
    assert parsed == ParsedTitle(party="R", office="Governor", state="RI", district="")


def test_parse_house_district():
    parsed = parse_title("Will the Democratic Party win the MI-07 House race?")
    assert parsed == ParsedTitle(party="D", office="House", state="MI", district="07")


def test_parse_chamber_control_senate():
    parsed = parse_title("Will the Republican Party win the U.S. Senate?")
    assert parsed == ParsedTitle(party="R", office="Senate", state="US", district="")


def test_parse_chamber_control_house():
    parsed = parse_title("Will the Democratic Party win the U.S. House?")
    assert parsed == ParsedTitle(party="D", office="House", state="US", district="")


def test_parse_independent_senate():
    parsed = parse_title("Will the Independent Party win the Nebraska Senate?")
    assert parsed == ParsedTitle(party="I", office="Senate", state="NE", district="")


def test_parse_independent_governor():
    parsed = parse_title("Will the Independent Party win the Rhode Island Governor?")
    assert parsed == ParsedTitle(party="I", office="Governor", state="RI", district="")


def test_unrecognized_title_raises():
    with pytest.raises(ValueError, match="does not match any known"):
        parse_title("Will unemployment be below 4% on Jan 1, 2026?")


def test_unrecognized_state_name_raises():
    with pytest.raises(ValueError, match="unrecognized state name"):
        parse_title("Will the Republican Party win the Atlantis Senate?")


def test_house_district_state_code_must_be_two_letters():
    # Regression: a malformed district code shouldn't silently fall through
    # to the generic Senate/Governor patterns and mis-parse.
    with pytest.raises(ValueError, match="does not match any known"):
        parse_title("Will the Republican Party win the MICH-07 House race?")


def test_race_key_groups_senate_across_parties():
    r = parse_title("Will the Republican Party win the Nebraska Senate?")
    d = parse_title("Will the Democratic Party win the Nebraska Senate?")
    i = parse_title("Will the Independent Party win the Nebraska Senate?")
    assert race_key(r) == race_key(d) == race_key(i)


def test_race_key_includes_district_for_house():
    a = parse_title("Will the Republican Party win the MI-07 House race?")
    b = parse_title("Will the Democratic Party win the MI-07 House race?")
    other_district = parse_title("Will the Republican Party win the MI-10 House race?")
    assert race_key(a) == race_key(b)
    assert race_key(a) != race_key(other_district)


def test_race_key_distinguishes_chamber_control_from_any_state():
    chamber = parse_title("Will the Republican Party win the U.S. Senate?")
    # No real state is coded "US", so this can't collide, but assert it
    # explicitly since it's the whole basis for not needing a special case.
    assert chamber.state == "US"
    assert race_key(chamber) == "US-Senate"


def test_race_key_distinguishes_offices_in_same_state():
    senate = parse_title("Will the Republican Party win the Michigan Senate?")
    governor = parse_title("Will the Republican Party win the Michigan Governor?")
    assert race_key(senate) != race_key(governor)


def test_all_real_cup_market_titles_parse_without_error():
    markets = json.loads(FIXTURE.read_text())
    assert len(markets) == 237

    races = {}
    for m in markets:
        parsed = parse_title(m["title"])
        races.setdefault(race_key(parsed), []).append(parsed.party)

    assert len(races) == 117
    sizes = sorted(len(v) for v in races.values())
    assert sizes.count(2) == 114
    assert sizes.count(3) == 3


def test_build_rows_shape():
    markets = json.loads(FIXTURE.read_text())[:3]
    rows = build_rows(markets)
    assert len(rows) == 3
    for row in rows:
        assert set(row.keys()) == {
            "id",
            "exchange_id",
            "title",
            "category",
            "state",
            "office",
            "district",
            "party",
            "race_key",
        }
