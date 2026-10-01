"""Pure matching logic for drafting config/market_map.csv: given a race's
office/state/district and a candidate external-platform event's markets,
pick out the sub-market for each party and score confidence. The live
fetch (Kalshi, Polymarket) lives in scripts/draft_market_map.py; this
module is the testable, side-effect-free part.

Deterministic string/keyword matching only, per the task that created this
file - no LLM (CLAUDE.md Hard Rule 2 applies to market mapping generally).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from predcup.cup_markets import STATE_ABBREVIATIONS
from predcup.venues.polymarket import PolymarketSchemaError, parse_gamma_market

ABBR_TO_STATE = {v: k for k, v in STATE_ABBREVIATIONS.items()}

PARTY_KEYWORDS = {
    "R": ("republican",),
    "D": ("democrat",),  # matches "Democrat" and "Democratic"
    "I": ("independent",),
}


@dataclass(frozen=True)
class ExternalMarket:
    """One outcome market on an external platform (Kalshi contract ticker
    or Polymarket clobTokenId), plus the text used to identify its party."""

    ref: str  # kalshi ticker, or Polymarket YES clobTokenId
    text: str  # the title/question text to match a party keyword against


def match_party(markets: list[ExternalMarket], party: str) -> ExternalMarket | None:
    """Find the sub-market whose text names `party` ("R"/"D"/"I")."""
    keywords = PARTY_KEYWORDS.get(party, ())
    for m in markets:
        text_lower = m.text.lower()
        if any(kw in text_lower for kw in keywords):
            return m
    return None


@dataclass(frozen=True)
class MatchRow:
    platform_id: str
    kalshi_ticker: str
    poly_token_id: str
    polarity: str
    confidence: float
    rule_diff_notes: str
    verified: bool = False


def build_row(
    platform_id: str,
    kalshi_ref: str | None,
    kalshi_confidence: float,
    kalshi_note: str,
    poly_ref: str | None,
    poly_confidence: float,
    poly_note: str,
) -> MatchRow:
    """Combine independent Kalshi and Polymarket match results into one
    market_map.csv row. Overall confidence is the average of whichever
    sides actually matched (0 counts if a side has no match at all) -
    deliberately conservative: a strong Kalshi match and a missing
    Polymarket match should not read as "fully mapped"."""
    notes = "; ".join(n for n in (kalshi_note, poly_note) if n)
    confidences = [c for c, ref in ((kalshi_confidence, kalshi_ref), (poly_confidence, poly_ref))]
    overall = round(sum(confidences) / len(confidences), 2) if confidences else 0.0
    polarity = "same" if (kalshi_ref or poly_ref) else ""
    return MatchRow(
        platform_id=platform_id,
        kalshi_ticker=kalshi_ref or "",
        poly_token_id=poly_ref or "",
        polarity=polarity,
        confidence=overall,
        rule_diff_notes=notes,
        verified=False,
    )


# --- Kalshi Senate (event-based) ------------------------------------------
#
# Kalshi's 2026 Senate general elections live in series SENATE<ST> (special
# elections: SENATE<ST>S; Louisiana: KXSENATELA, event -26NOV), event
# SENATE<ST>-26, markets -D / -R (independents: a candidate code). The YES
# label is the candidate's name, but the rules resolve on party ("a
# representative of the Democratic party is sworn in"), except independents,
# which are candidate-specific. The same series also holds 2028 events with
# party-named labels, and the ticker's state code can be wrong (SENATELA-26
# is titled "Kentucky Senate winner?"), so everything is keyed on the event
# title. The SENATEPARTY<ST> series the governor-style matcher used have no
# markets. Observed live 2026-09-30.

_SENATE_EXCLUDE = ("primary", "nominee", "nomination", "runoff", "caucus")
_YEAR_RE = re.compile(r"\b(20\d\d)\b")


def state_in_title(title: str) -> str | None:
    """USPS code of the (longest) US state name in `title`, or None."""
    for name in sorted(STATE_ABBREVIATIONS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", title, re.IGNORECASE):
            return STATE_ABBREVIATIONS[name]
    return None


def _active(markets: list[dict]) -> list[dict]:
    return [m for m in markets if m.get("status") == "active"]


def is_2026_general_event(event: dict) -> bool:
    """A 2026 Senate general-election event with at least one active market."""
    text = f"{event.get('title') or ''} {event.get('sub_title') or ''}"
    if any(kw in text.lower() for kw in _SENATE_EXCLUDE):
        return False
    if any(y != "2026" for y in _YEAR_RE.findall(text)):
        return False
    parts = event.get("event_ticker", "").split("-")
    if len(parts) < 2 or not parts[1].startswith("26"):
        return False
    return bool(_active(event.get("markets") or []))


def index_senate_events(events: list[dict]) -> dict[str, list[dict]]:
    """state -> 2026 Senate general events, state taken from the event title.
    More than one event for a state means ambiguous: the caller must not
    pick one."""
    index: dict[str, list[dict]] = {}
    for ev in events:
        if not is_2026_general_event(ev):
            continue
        state = state_in_title(ev.get("title") or "")
        if state:
            index.setdefault(state, []).append(ev)
    return index


def match_senate_party(markets: list[dict], party: str) -> ExternalMarket | None:
    """D/R: ticker suffix and title must agree. I: exactly one active market
    that names an independent and isn't the -D/-R market."""
    active = _active(markets)
    if party in ("D", "R"):
        word = "democrat" if party == "D" else "republican"
        hits = [
            m for m in active
            if m["ticker"].endswith(f"-{party}") and word in (m.get("title") or "").lower()
        ]  # fmt: skip
    elif party == "I":
        hits = [
            m for m in active
            if not m["ticker"].endswith(("-D", "-R"))
            and "independent" in f"{m.get('title') or ''} {m.get('subtitle') or ''}".lower()
        ]  # fmt: skip
    else:
        return None
    if len(hits) != 1:
        return None
    return ExternalMarket(ref=hits[0]["ticker"], text=hits[0].get("title") or "")


# --- Fusion risk ------------------------------------------------------------


def fusion_race_keys(cup_rows: list[dict[str, str]], map_rows: list[dict[str, str]]) -> frozenset[str]:
    """Races where any market_map.csv row has fusion_risk=true. In such a
    race a fusion candidate counts for every party on the ticket (SIG
    rules), so R and D markets are not complements: risk.py keeps them off
    the R-vs-D axis and the parity scanner skips the race. Anything other
    than "true"/"false" raises rather than being read as false."""
    race_of = {c["id"]: c["race_key"] for c in cup_rows}
    keys: set[str] = set()
    for r in map_rows:
        if "fusion_risk" not in r:
            raise ValueError("market_map.csv has no fusion_risk column")
        value = (r["fusion_risk"] or "").strip().lower()
        if value not in ("true", "false"):
            raise ValueError(f"fusion_risk must be true or false, got {r['fusion_risk']!r} (platform_id {r['platform_id']})")
        if value == "true":
            if r["platform_id"] not in race_of:
                raise ValueError(f"fusion_risk row {r['platform_id']} is not in the Cup market list")
            keys.add(race_of[r["platform_id"]])
    return frozenset(keys)


def prefer_2026_event_markets(markets: list[dict]) -> list[dict]:
    """A Kalshi series can hold several cycles (GOVPARTYVT-26 and -28). Keep
    only the markets of -26 events when any exist; otherwise return all of
    them unchanged (GOVPARTYNH-28 is the 2026 race despite its ticker), and
    leave the year question to show_mapping's warnings."""
    def is_2026(m: dict) -> bool:
        parts = m.get("event_ticker", "").split("-")
        return len(parts) > 1 and (parts[1].startswith("26") or parts[1].startswith("2026"))

    cycle = [m for m in markets if is_2026(m)]
    return cycle or markets


# --- Polymarket (Gamma public-search events) ---------------------------------
#
# Polymarket's 2026 general-election winner markets (observed live
# 2026-10-01) are one binary market per party inside a negRisk event:
#   "<State> Senate Election Winner":   Will the Republicans win the Texas Senate race in 2026?
#   "<State> Governor Election Winner": Will the Democrats win the Nevada governor race in 2026?
#                                       Will an independent win the Michigan governor race in 2026?
#   "<ST>-<DD> House Election Winner":  Will the Democratic Party win the PA-07 House seat?
#   "Which party will win the Senate in 2026?":
#       Will the Republican Party control the Senate after the 2026 Midterm elections?
# Around them sit look-alikes that must never match: margin-of-victory
# brackets, county winners, turnout, "within 5%", state-legislature control
# ("control the Texas Senate"), primaries, candidate-specific markets and
# inactive "Person A" placeholders. So the whole question must fit the
# race's pattern exactly; the party is read from the question only (never
# the slug or groupItemTitle, which names a candidate); more than one
# qualifying market is ambiguous and gives no match.

_POLY_PARTY = {
    "D": r"the Democrats|the Democratic Party",
    "R": r"the Republicans|the Republican Party",
    "I": r"an independent|an independent candidate",
}
_POLY_EVENT_EXCLUDE = ("primary", "nominee", "nomination", "runoff", "caucus", "margin", "county", "turnout")
_POLY_YEAR = 2026


def poly_search_query(office: str, state: str, district: str) -> str:
    """Gamma /public-search query for a race's winner event."""
    if state == "US":
        return f"Which party will win the {office} in 2026?"
    if office == "House":
        return f"{state}-{int(district):02d} House Election Winner"
    return f"{ABBR_TO_STATE[state]} {office} Election Winner"


def _poly_question_re(office: str, state: str, district: str, party: str) -> re.Pattern | None:
    who = _POLY_PARTY.get(party)
    if who is None:
        return None
    if state == "US":
        if party == "I":
            return None
        body = rf"Will (?:{who}) control the {office} after the 2026 Midterm elections\?"
    elif office == "House":
        body = rf"Will (?:{who}) win the {state}-{int(district):02d} House seat\?"
    elif office in ("Senate", "Governor"):
        body = rf"Will (?:{who}) win the {re.escape(ABBR_TO_STATE[state])} {office} race in 2026\?"
    else:
        return None
    return re.compile(body, re.IGNORECASE)


def _is_2026(market) -> bool:
    """Every year named in the question is 2026, and the question or (for
    year-less House questions) the end date puts it in 2026."""
    years = {int(y) for y in _YEAR_RE.findall(market.question)}
    if years - {_POLY_YEAR}:
        return False
    return bool(years) or (market.end_date is not None and market.end_date.year == _POLY_YEAR)


def match_poly_party(
    events: list[dict], office: str, state: str, district: str, party: str
) -> tuple[ExternalMarket | None, str]:
    """(YES-token match or None, note). Exactly one live, binary, 2026,
    pattern-exact market across all `events`, or no match."""
    pattern = _poly_question_re(office, state, district, party)
    if pattern is None:
        return None, f"no Polymarket pattern for {office} {state} party {party}"
    hits: dict[str, ExternalMarket] = {}
    for ev in events:
        if ev.get("closed") or any(kw in (ev.get("title") or "").lower() for kw in _POLY_EVENT_EXCLUDE):
            continue
        for raw in ev.get("markets") or []:
            if raw.get("active") is not True or raw.get("closed") is not False:
                continue
            if not pattern.fullmatch((raw.get("question") or "").strip()):
                continue
            try:
                m = parse_gamma_market(raw)
            except (PolymarketSchemaError, ValueError, KeyError):
                continue
            if _is_2026(m):
                hits[m.yes_token_id] = ExternalMarket(ref=m.yes_token_id, text=m.question)
    if len(hits) == 1:
        return next(iter(hits.values())), ""
    if not hits:
        return None, f"no Polymarket 2026 general market for {office} {state}{district} {party}"
    return None, f"Polymarket ambiguous: {len(hits)} markets fit {office} {state}{district} {party}"


def _without_poly_notes(notes: str) -> list[str]:
    return [n for n in (p.strip() for p in notes.split(";")) if n and "polymarket" not in n.lower()]


def apply_poly_column(
    map_rows: list[dict[str, str]], results: dict[str, tuple[ExternalMarket | None, str]]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Write Polymarket matches into market_map.csv rows. A verified=true
    row changes in poly_token_id only and is listed in the returned review
    (a human verified its Kalshi side, not this); an unverified row also
    gets its Polymarket note refreshed. Confidence is never changed here.
    Rows with no result are returned unchanged."""
    out: list[dict[str, str]] = []
    review: list[dict[str, str]] = []
    for row in map_rows:
        res = results.get(row["platform_id"])
        if res is None:
            out.append(row)
            continue
        hit, note = res
        new = {**row, "poly_token_id": hit.ref if hit else ""}
        if row.get("verified", "").strip().lower() == "true":
            review.append({
                "platform_id": row["platform_id"], "kalshi_ticker": row.get("kalshi_ticker", ""),
                "old_poly_token_id": row.get("poly_token_id", ""), "new_poly_token_id": new["poly_token_id"],
                "new_question": hit.text if hit else "", "note": note,
            })  # fmt: skip
        else:
            new["rule_diff_notes"] = "; ".join(_without_poly_notes(row.get("rule_diff_notes", "")) + ([note] if note else []))
        out.append(new)
    return out, review
