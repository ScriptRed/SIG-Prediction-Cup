from predcup.market_map import ExternalMarket, MatchRow, build_row, match_party


def test_match_party_republican():
    markets = [
        ExternalMarket(ref="GOVPARTYMI-26-D", text="Will the Democrats win the Michigan governor race?"),
        ExternalMarket(ref="GOVPARTYMI-26-R", text="Will the Republicans win the Michigan governor race?"),
    ]
    assert match_party(markets, "R").ref == "GOVPARTYMI-26-R"


def test_match_party_democrat_matches_democratic_too():
    markets = [ExternalMarket(ref="tok-1", text="Will the Democratic Party win the Ohio Senate?")]
    assert match_party(markets, "D").ref == "tok-1"


def test_match_party_independent():
    markets = [
        ExternalMarket(ref="tok-r", text="Will the Republican win?"),
        ExternalMarket(ref="tok-i", text="Will an independent win the Rhode Island governor race?"),
    ]
    assert match_party(markets, "I").ref == "tok-i"


def test_match_party_no_match_returns_none():
    markets = [ExternalMarket(ref="tok-r", text="Will the Republican win?")]
    assert match_party(markets, "D") is None


def test_match_party_case_insensitive():
    markets = [ExternalMarket(ref="tok-1", text="WILL THE DEMOCRATIC PARTY WIN?")]
    assert match_party(markets, "D") is not None


def test_build_row_both_platforms_matched():
    row = build_row(
        platform_id="386",
        kalshi_ref="GOVPARTYMI-26-R",
        kalshi_confidence=0.9,
        kalshi_note="",
        poly_ref="123456",
        poly_confidence=0.8,
        poly_note="",
    )
    assert row.kalshi_ticker == "GOVPARTYMI-26-R"
    assert row.poly_token_id == "123456"
    assert row.polarity == "same"
    assert row.confidence == 0.85
    assert row.verified is False


def test_build_row_only_kalshi_matched_confidence_is_averaged_with_zero():
    # A strong Kalshi match with no Polymarket match at all must not read
    # as "fully mapped" -- confidence should reflect the gap.
    row = build_row(
        platform_id="386",
        kalshi_ref="GOVPARTYMI-26-R",
        kalshi_confidence=0.9,
        kalshi_note="",
        poly_ref=None,
        poly_confidence=0.0,
        poly_note="no Polymarket event found",
    )
    assert row.confidence == 0.45
    assert row.poly_token_id == ""
    assert "no Polymarket event found" in row.rule_diff_notes


def test_build_row_neither_matched():
    row = build_row(
        platform_id="999",
        kalshi_ref=None,
        kalshi_confidence=0.0,
        kalshi_note="no Kalshi series found",
        poly_ref=None,
        poly_confidence=0.0,
        poly_note="no Polymarket event found",
    )
    assert row.confidence == 0.0
    assert row.polarity == ""
    assert row.kalshi_ticker == ""
    assert row.poly_token_id == ""


def test_build_row_notes_join_only_nonempty():
    row = build_row(
        platform_id="1",
        kalshi_ref="X",
        kalshi_confidence=1.0,
        kalshi_note="",
        poly_ref="Y",
        poly_confidence=1.0,
        poly_note="",
    )
    assert row.rule_diff_notes == ""


def test_match_row_default_unverified():
    row = MatchRow(
        platform_id="1", kalshi_ticker="X", poly_token_id="Y",
        polarity="same", confidence=1.0, rule_diff_notes="",
    )
    assert row.verified is False


# --- fusion_risk --------------------------------------------------------------

import pytest  # noqa: E402

from predcup.market_map import fusion_race_keys  # noqa: E402

_CUP = [
    {"id": "1", "race_key": "NY-Senate"},
    {"id": "2", "race_key": "NY-Senate"},
    {"id": "3", "race_key": "MI-Senate"},
]


def _map(*values):
    return [{"platform_id": str(i + 1), "fusion_risk": v} for i, v in enumerate(values)]


def test_fusion_race_keys_flags_whole_race_if_any_row_true():
    assert fusion_race_keys(_CUP, _map("false", "true", "false")) == frozenset({"NY-Senate"})


def test_fusion_race_keys_default_false():
    assert fusion_race_keys(_CUP, _map("false", "false", "false")) == frozenset()


def test_fusion_race_keys_rejects_unknown_value():
    with pytest.raises(ValueError, match="true or false"):
        fusion_race_keys(_CUP, _map("false", "yes", "false"))


def test_fusion_race_keys_rejects_missing_column():
    with pytest.raises(ValueError, match="no fusion_risk column"):
        fusion_race_keys(_CUP, [{"platform_id": "1"}])


# --- governor series holding several cycles (GOVPARTYVT-26 and -28) ---------

from predcup.market_map import prefer_2026_event_markets  # noqa: E402


def test_prefer_2026_event_markets_drops_other_cycles_when_2026_exists():
    markets = [
        {"ticker": "GOVPARTYVT-28-D", "event_ticker": "GOVPARTYVT-28"},
        {"ticker": "GOVPARTYVT-26-D", "event_ticker": "GOVPARTYVT-26"},
        {"ticker": "GOVPARTYVT-26-R", "event_ticker": "GOVPARTYVT-26"},
    ]
    assert [m["ticker"] for m in prefer_2026_event_markets(markets)] == ["GOVPARTYVT-26-D", "GOVPARTYVT-26-R"]


def test_prefer_2026_event_markets_keeps_all_when_no_2026_event():
    # NH: Kalshi's only event is GOVPARTYNH-28, which is the 2026 race by its
    # rules. Nothing is dropped; show_mapping flags the ticker for a human.
    markets = [{"ticker": "GOVPARTYNH-28-D", "event_ticker": "GOVPARTYNH-28"}]
    assert prefer_2026_event_markets(markets) == markets
