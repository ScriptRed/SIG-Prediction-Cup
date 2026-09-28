"""Pure matching logic for drafting config/market_map.csv: given a race's
office/state/district and a candidate external-platform event's markets,
pick out the sub-market for each party and score confidence. The live
fetch (Kalshi, Polymarket) lives in scripts/draft_market_map.py; this
module is the testable, side-effect-free part.

Deterministic string/keyword matching only, per the task that created this
file - no LLM (CLAUDE.md Hard Rule 2 applies to market mapping generally).
"""

from __future__ import annotations

from dataclasses import dataclass

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
