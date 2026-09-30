import pytest

from predcup.overround import (
    HIGH_FLAG_POINTS,
    LOW_FLAG_POINTS,
    RaceSummary,
    representative_price,
    summarize_race,
)


def test_representative_price_prefers_latest_trade():
    assert representative_price(0.5, 0.1, 0.9) == 0.5


def test_representative_price_mid_of_bid_and_ask():
    assert representative_price(None, 0.40, 0.44) == pytest.approx(0.42)


def test_representative_price_bid_only():
    assert representative_price(None, 0.95, None) == 0.95


def test_representative_price_ask_only():
    assert representative_price(None, None, 0.055) == 0.055


def test_representative_price_none_when_book_empty():
    assert representative_price(None, None, None) is None


def test_summarize_race_ok_within_band():
    summary = summarize_race("MI-Senate", {"R": 0.45, "D": 0.55})
    assert summary.status == "ok"
    assert summary.sum_points == 100.0


def test_summarize_race_flagged_above_high():
    summary = summarize_race("MI-Senate", {"R": 0.55, "D": 0.55})
    assert summary.status == "flagged"
    assert summary.sum_points == 110.0


def test_summarize_race_flagged_below_low():
    summary = summarize_race("MI-Senate", {"R": 0.40, "D": 0.45})
    assert summary.status == "flagged"
    assert summary.sum_points == 85.0


def test_summarize_race_boundary_exactly_101_is_not_flagged():
    summary = summarize_race("MI-Senate", {"R": 0.51, "D": 0.50})
    assert summary.sum_points == HIGH_FLAG_POINTS
    assert summary.status == "ok"


def test_summarize_race_boundary_exactly_99_is_not_flagged():
    summary = summarize_race("MI-Senate", {"R": 0.49, "D": 0.50})
    assert summary.sum_points == LOW_FLAG_POINTS
    assert summary.status == "ok"


def test_summarize_race_just_over_101_is_flagged():
    summary = summarize_race("MI-Senate", {"R": 0.511, "D": 0.50})
    assert summary.status == "flagged"


def test_summarize_race_three_way_ok():
    summary = summarize_race("NE-Senate", {"R": 0.30, "D": 0.20, "I": 0.49})
    assert summary.status == "ok"
    assert summary.sum_points == 99.0


def test_summarize_race_missing_price_is_insufficient_data_not_zero():
    # A missing party must never be silently treated as price 0 -- that
    # would corrupt the sum and could mask a real overround/underround.
    summary = summarize_race("RI-Governor", {"R": 0.10, "D": 0.95, "I": None})
    assert summary.status == "insufficient_data"
    assert summary.sum_points is None
    assert summary.missing_parties == ("I",)
    assert summary.prices == {"R": 0.10, "D": 0.95}


def test_summarize_race_all_missing():
    summary = summarize_race("XX-Senate", {"R": None, "D": None})
    assert summary.status == "insufficient_data"
    assert summary.missing_parties == ("D", "R")


def test_race_summary_is_frozen():
    summary = summarize_race("MI-Senate", {"R": 0.45, "D": 0.55})
    with pytest.raises(Exception):
        summary.status = "ok"


def test_scan_skips_fusion_races_without_reading_their_books(monkeypatch):
    import httpx

    from scripts.scan_race_overround import scan

    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        requested.append(request.url.path)
        return httpx.Response(200, json={"latestPrice": None, "bestBid": 0.49, "bestAsk": 0.51})

    races = {
        "NY-Senate": [{"party": "R", "exchange_id": "1"}, {"party": "D", "exchange_id": "2"}],
        "MI-Senate": [{"party": "R", "exchange_id": "3"}, {"party": "D", "exchange_id": "4"}],
    }
    import scripts.scan_race_overround as mod

    monkeypatch.setattr(mod, "REQUEST_DELAY_SECONDS", 0)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = {r.race_key: r for r in scan(races, client, {}, "tid", frozenset({"NY-Senate"}))}
    assert results["NY-Senate"].status == "skipped_fusion"
    assert results["MI-Senate"].status == "ok"
    assert all("/exchanges/1/" not in p and "/exchanges/2/" not in p for p in requested)
    assert len(requested) == 2
