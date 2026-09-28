"""Parse Cup market titles into (party, office, state, district) and group
them into races. Deterministic regex only — no LLM (CLAUDE.md Hard Rule 2:
LLM output may only produce alerts/flags, never feed mapping/sizing logic).

Every title that doesn't match a known pattern raises ValueError rather
than being silently guessed, per the project's "stop and ask, don't invent"
rule for anything not confirmed against real platform data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

PARTY_CODES = {"Republican": "R", "Democratic": "D", "Independent": "I"}

STATE_ABBREVIATIONS = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}  # fmt: skip

_PARTY_ALTERNATION = "|".join(PARTY_CODES)
_CHAMBER_RE = re.compile(
    rf"^Will the ({_PARTY_ALTERNATION}) Party win the U\.S\. (Senate|House)\?$"
)
_HOUSE_RE = re.compile(
    rf"^Will the ({_PARTY_ALTERNATION}) Party win the ([A-Z]{{2}})-(\d{{2}}) House race\?$"
)
_GOVERNOR_RE = re.compile(
    rf"^Will the ({_PARTY_ALTERNATION}) Party win the (.+) Governor\?$"
)
_SENATE_RE = re.compile(
    rf"^Will the ({_PARTY_ALTERNATION}) Party win the (.+) Senate\?$"
)


@dataclass(frozen=True)
class ParsedTitle:
    party: str  # "R" / "D" / "I"
    office: str  # "Senate" / "Governor" / "House"
    state: str  # 2-letter USPS code, or "US" for chamber-control races
    district: str  # 2-digit zero-padded House district, "" otherwise


def _state_code(state_name: str, title: str) -> str:
    if state_name not in STATE_ABBREVIATIONS:
        raise ValueError(f"unrecognized state name {state_name!r} in title: {title!r}")
    return STATE_ABBREVIATIONS[state_name]


def parse_title(title: str) -> ParsedTitle:
    m = _CHAMBER_RE.match(title)
    if m:
        party_word, chamber = m.groups()
        return ParsedTitle(party=PARTY_CODES[party_word], office=chamber, state="US", district="")

    m = _HOUSE_RE.match(title)
    if m:
        party_word, state, district = m.groups()
        return ParsedTitle(party=PARTY_CODES[party_word], office="House", state=state, district=district)

    m = _GOVERNOR_RE.match(title)
    if m:
        party_word, state_name = m.groups()
        return ParsedTitle(
            party=PARTY_CODES[party_word],
            office="Governor",
            state=_state_code(state_name, title),
            district="",
        )

    m = _SENATE_RE.match(title)
    if m:
        party_word, state_name = m.groups()
        return ParsedTitle(
            party=PARTY_CODES[party_word],
            office="Senate",
            state=_state_code(state_name, title),
            district="",
        )

    raise ValueError(f"title does not match any known Cup market pattern: {title!r}")


def race_key(parsed: ParsedTitle) -> str:
    """Groups the R/D/I markets of one race. No real state is coded "US",
    so chamber-control races (state="US") can't collide with any state
    race without a separate case."""
    if parsed.district:
        return f"{parsed.state}-{parsed.office}-{parsed.district}"
    return f"{parsed.state}-{parsed.office}"


def build_rows(raw_markets: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for m in raw_markets:
        parsed = parse_title(m["title"])
        exchange_id = m["exchanges"][0]["id"] if m["exchanges"] else ""
        rows.append(
            {
                "id": m["id"],
                "exchange_id": exchange_id,
                "title": m["title"],
                "category": ";".join(m["categories"]),
                "state": parsed.state,
                "office": parsed.office,
                "district": parsed.district,
                "party": parsed.party,
                "race_key": race_key(parsed),
            }
        )
    return rows
