"""scripts/launch_report.py and predcup/launch_report.py. Kalshi payloads
are synthetic (shaped per docs/kalshi/openapi.yaml) except the SENATELA-26
state-mismatch case, which uses the real 2026-09-30 fixture."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import httpx
import pytest

from predcup.launch_report import (
    LaunchReportConfig,
    SigTop,
    build_row,
    kalshi_party,
    sig_top_from_orderbook,
    sort_rows,
    summarize,
)
from predcup.venues.kalshi import parse_event, parse_market
from scripts import launch_report

CFG = LaunchReportConfig(
    max_gap=0.03,
    longshot_below=0.10,
    min_kalshi_volume=1000,
    exclude_states=frozenset({"AK", "GA", "ME"}),
    exclude_independent=True,
)
TWO_SIDED = SigTop(bid=0.60, bid_size=100, ask=0.64, ask_size=50)


def km(ticker="SENATEAZ-26-D", party="Democratic", bid="0.6100", ask="0.6300", volume="50000.00", **kw) -> dict:
    raw = {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "title": f"Will {party}s win the Senate race in Arizona?",
        "subtitle": f"{party} party:: {party} party",
        "yes_sub_title": "Jane Candidate",
        "no_sub_title": "Jane Candidate",
        "status": "active",
        "close_time": "2027-11-03T15:00:00Z",
        "expected_expiration_time": "2027-01-04T15:00:00Z",
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "last_price_dollars": bid,
        "volume_fp": volume,
        "volume_24h_fp": "0.00",
        "rules_primary": f"If a representative of the {party} party is sworn in as a Senator of Arizona "
        "for the term beginning in 2027, then the market resolves to Yes.",
        "rules_secondary": "",
    }
    raw.update(kw)
    return raw


def ev(title="Arizona Senate winner?", ticker="SENATEAZ-26"):
    return parse_event({"event_ticker": ticker, "series_ticker": ticker.split("-")[0], "title": title,
                        "sub_title": "In 2026", "settlement_sources": []})  # fmt: skip


def cup(state="AZ", party="D", office="Senate", mid="901"):
    return {"id": mid, "exchange_id": "1" + mid, "title": f"Will the X Party win the {state} {office}?",
            "state": state, "office": office, "party": party, "race_key": f"{state}-{office}"}  # fmt: skip


MAP = {"kalshi_ticker": "SENATEAZ-26-D", "polarity": "same"}


def row(cup_row=None, sig=TWO_SIDED, raw=None, event=None, map_row=MAP):
    return build_row(cup_row or cup(), map_row, sig, parse_market(raw or km()), event or ev(), CFG)


def test_clean_row_has_no_flags_and_signed_gap():
    r = row()
    assert r.flags == []
    assert r.sig_mid == pytest.approx(0.62) and r.sig_spread == pytest.approx(0.04)
    assert r.gap == pytest.approx(0.0)
    assert r.kalshi_party == "D" and r.kalshi_label == "Jane Candidate"


def test_party_mismatch():
    r = row(raw=km(party="Republican"))
    assert "party mismatch: SIG D, Kalshi R" in r.flags


def test_kalshi_party_from_rules_for_chamber_control():
    raw = km(ticker="CONTROLS-2026-D", rules_primary="If the Democratic Party has won control of the U.S. Senate in 2026, then the market resolves to Yes.")
    assert kalshi_party(parse_market(raw)) == "D"


def test_state_mismatch_on_real_senatela_kentucky_event():
    fixture = Path(__file__).parent / "fixtures" / "kalshi_senate_events_2026-09-30.json"
    real = next(e for e in json.loads(fixture.read_text())["SENATELA"] if e["event_ticker"] == "SENATELA-26")
    m = next(m for m in real["markets"] if m["ticker"] == "SENATELA-26-R")
    raw = km(**{k: m[k] for k in ("ticker", "event_ticker", "title", "subtitle", "yes_sub_title", "rules_primary")})
    r = row(cup_row=cup(state="LA", party="R"), raw=raw, event=parse_event(real), map_row={"kalshi_ticker": m["ticker"], "polarity": "same"})
    assert "state mismatch: event title is KY, SIG LA" in r.flags
    ok = row(cup_row=cup(state="KY", party="R"), raw=raw, event=parse_event(real), map_row={"kalshi_ticker": m["ticker"], "polarity": "same"})
    assert not any("state" in f for f in ok.flags)


def test_gap_over_threshold():
    r = row(raw=km(bid="0.5500", ask="0.5700"))  # Kalshi 0.56 vs SIG 0.62
    assert r.gap == pytest.approx(0.06)
    assert "gap +6.0 pts" in r.flags


def test_gap_under_threshold_is_not_flagged():
    r = row(raw=km(bid="0.5800", ask="0.6020"))  # Kalshi 0.591 vs SIG 0.62: 2.9 points
    assert not any(f.startswith("gap") for f in r.flags)


def test_inverted_polarity_gap_uses_complement():
    r = row(raw=km(bid="0.3700", ask="0.3900"), map_row={"kalshi_ticker": "X", "polarity": "inverted"})
    assert r.kalshi_mid_adj == pytest.approx(0.62) and r.gap == pytest.approx(0.0)
    assert "inverted polarity: check party by hand" in r.flags


def test_empty_and_one_sided_books():
    assert "SIG book empty" in row(sig=SigTop(None, None, None, None)).flags
    assert "SIG book one-sided" in row(sig=SigTop(None, None, 0.05, 10)).flags
    assert "Kalshi book one-sided" in row(raw=km(bid="0.0000")).flags


def test_low_volume():
    assert "Kalshi volume low (12)" in row(raw=km(volume="12.00")).flags


@pytest.mark.parametrize("state", ["AK", "GA", "ME"])
def test_excluded_states(state):
    r = row(cup_row=cup(state=state), event=ev(title={"AK": "Alaska", "GA": "Georgia", "ME": "Maine"}[state] + " Senate winner?"))
    assert f"excluded state {state}" in r.flags


def test_independent_market_excluded():
    raw = km(title="Will Dan Osborn (as an independent)s win the Senate race in Arizona?",
             rules_primary="If Dan Osborn (as an independent) is sworn in as a Senator of Arizona, then Yes.")  # fmt: skip
    r = row(cup_row=cup(party="I"), raw=raw)
    assert "excluded: Independent market" in r.flags
    assert r.kalshi_party == "I"


def test_longshot_overpricing_only_under_threshold():
    cheap = row(sig=SigTop(0.005, 10, 0.06, 20), raw=km(bid="0.0100", ask="0.0140"))
    assert cheap.longshot_overpricing == pytest.approx(0.06 - 0.012)
    assert row().longshot_overpricing is None


def test_sort_by_absolute_gap_none_last():
    a = row(raw=km(bid="0.5500", ask="0.5700"))  # +6
    b = row(raw=km(bid="0.6900", ask="0.7100"))  # -8
    c = row(sig=SigTop(None, None, None, None))  # no gap
    assert [r.gap for r in sort_rows([c, a, b])][:2] == [pytest.approx(-0.08), pytest.approx(0.06)]
    assert sort_rows([c, a, b])[-1] is c


def test_summary_clean_flagged_and_longshots():
    clean = row(cup_row=cup(state="AZ"))
    flagged = row(cup_row=cup(state="CO", mid="902"), event=ev(title="Colorado Senate winner?"), raw=km(volume="5.00"))
    s = summarize([clean, flagged])
    assert s.clean_races == ["AZ-Senate"]
    assert s.flagged_races == {"CO-Senate": ["D: Kalshi volume low (5)"]}


def test_sig_top_from_orderbook():
    top = sig_top_from_orderbook({"bestBid": 0.4, "bestAsk": 0.45, "bids": [{"price": 0.4, "quantity": 300}],
                                  "asks": [{"price": 0.45, "quantity": 20}]})  # fmt: skip
    assert top == SigTop(0.4, 300, 0.45, 20)


# --- end to end, GET-only ---------------------------------------------------

SIG = "https://sig.test/api/v1"
KALSHI = "https://kalshi.test/trade-api/v2"
TID = "22222222-2222-2222-2222-222222222222"


def handler(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET", f"launch_report must be read-only, got {request.method}"
    url, path = str(request.url), request.url.path
    if url.startswith(SIG):
        if path.endswith("/tournaments/test-cup"):
            return httpx.Response(200, json={"id": TID})
        assert request.url.params.get("tournamentId") == TID
        assert path.endswith("/orderbook")
        return httpx.Response(200, json={"exchangeId": "x", "marketId": "y", "depth": 1, "bestBid": 0.60, "bestAsk": 0.64,
                                         "spread": 0.04, "bids": [{"price": 0.60, "quantity": 100}],
                                         "asks": [{"price": 0.64, "quantity": 50}]})  # fmt: skip
    if "/markets/" in path:
        return httpx.Response(200, json={"market": km(ticker=path.split("/")[-1])})
    if "/events/" in path:
        return httpx.Response(200, json={"event": {"event_ticker": path.split("/")[-1], "series_ticker": "S",
                                                   "title": "Arizona Senate winner?", "sub_title": "In 2026"}})  # fmt: skip
    return httpx.Response(404)


def test_end_to_end_writes_csv_and_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("SIG_API_KEY", "k")
    (tmp_path / "cup.csv").write_text(
        "id,exchange_id,title,category,state,office,district,party,race_key\n"
        "901,1901,Will the Democratic Party win the Arizona Senate?,Election Outcome,AZ,Senate,,D,AZ-Senate\n"
        "902,1902,Will the Republican Party win the Arizona Senate?,Election Outcome,AZ,Senate,,R,AZ-Senate\n"
        "903,1903,Will the Democratic Party win the Colorado Senate?,Election Outcome,CO,Senate,,D,CO-Senate\n"
    )
    (tmp_path / "map.csv").write_text(
        "platform_id,kalshi_ticker,poly_token_id,polarity,rule_diff_notes,confidence,verified,tier\n"
        "901,SENATEAZ-26-D,,same,,0.8,false,\n"
        "902,SENATEAZ-26-R,,same,,0.8,false,\n"
        "903,,,,,0.0,false,\n"
    )
    (tmp_path / "s.yaml").write_text(
        f'platform: {{base_url: "{SIG}", tournament_slug: "test-cup"}}\n'
        f'venues: {{kalshi: {{base_url: "{KALSHI}", request_delay_seconds: 0}}, sig: {{request_delay_seconds: 0}}}}\n'
        "mapping_review: {min_kalshi_volume: 1000}\n"
        "launch_report: {max_gap: 0.03, longshot_below: 0.10, exclude_states: [AK, GA, ME], "
        "exclude_independent: true, output_path: unused.csv}\n"
    )
    lines: list[str] = []
    code = launch_report.main(
        [], transport=httpx.MockTransport(handler), out=lines.append,
        map_path=tmp_path / "map.csv", markets_path=tmp_path / "cup.csv",
        settings_path=tmp_path / "s.yaml", output_path=tmp_path / "report.csv",
    )  # fmt: skip
    assert code == 0
    rows = list(csv.DictReader(open(tmp_path / "report.csv")))
    assert [r["sig_market_id"] for r in rows] == ["901", "902"]  # 903 has no Kalshi ticker
    by_id = {r["sig_market_id"]: r for r in rows}
    # The mock serves a Democratic Kalshi market for every ticker, so the R row is a party mismatch.
    assert by_id["901"]["flags"] == ""
    assert "party mismatch: SIG R, Kalshi D" in by_id["902"]["flags"]
    text = "\n".join(lines)
    assert "1 races with Kalshi tickers (2 SIG markets)" in text
    assert "CLEAN: 0 races" in text and "AZ-Senate: R: party mismatch" in text


def test_minnesota_dfl_party_is_democratic():
    raw = km(rules_primary="If a representative of the Democratic (DFL) party is sworn in as a Senator of "
             "Minnesota for the term beginning in 2027, then the market resolves to Yes.")  # fmt: skip
    assert kalshi_party(parse_market(raw)) == "D"


def test_february_2027_expiry_is_still_2026_cycle():
    assert not any(f.startswith("not 2026") for f in row(raw=km(expected_expiration_time="2027-02-01T15:00:00Z")).flags)
    late = row(raw=km(expected_expiration_time="2029-01-09T15:00:00Z"))
    assert any("outside the 2026 cycle" in f for f in late.flags)
