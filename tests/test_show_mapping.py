"""scripts/show_mapping.py: fixture races served through a mock HTTP
transport that fails any non-GET request (the tool is read-only against
both venues). Kalshi payloads are synthetic, shaped per
docs/kalshi/openapi.yaml (GetMarketResponse / GetEventResponse)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from predcup.mapping_review import ReviewThresholds, review_warnings
from predcup.venues.kalshi import parse_market
from scripts import show_mapping

SIG = "https://sig.test/api/v1"
KALSHI = "https://kalshi.test/trade-api/v2"
TOURNAMENT_ID = "11111111-1111-1111-1111-111111111111"
TH = ReviewThresholds(max_mid_diff=0.10, max_kalshi_spread=0.05, min_kalshi_volume=1000)

CUP_CSV = """id,exchange_id,title,category,state,office,district,party,race_key
901,1901,Will the Democratic Party win the Arizona Senate?,Election Outcome,AZ,Senate,,D,AZ-Senate
902,1902,Will the Republican Party win the Arizona Senate?,Election Outcome,AZ,Senate,,R,AZ-Senate
911,1911,Will the Democratic Party win the Colorado Senate?,Election Outcome,CO,Senate,,D,CO-Senate
921,1921,Will the Democratic Party win the Connecticut Governor?,Election Outcome,CT,Governor,,D,CT-Governor
931,1931,Will the Democratic Party win the Delaware Senate?,Election Outcome,DE,Senate,,D,DE-Senate
"""

# CO-Senate's D row is mapped to Kalshi's *Republican* contract with
# polarity "same": effectively inverted, which the mid check must catch.
MAP_CSV = """platform_id,kalshi_ticker,poly_token_id,polarity,rule_diff_notes,confidence,verified,tier
901,SENATEAZ-26-D,,same,,0.9,false,
902,SENATEAZ-26-R,,same,"notes, with a comma",0.9,false,
911,SENATECO-26-R,,same,,0.9,false,
921,GOVPRIMARYCT-26-D,,same,,0.9,false,
931,,,,no Kalshi series,0.0,false,
"""

SETTINGS = f"""platform:
  base_url: "{SIG}"
  tournament_slug: "test-cup"
venues:
  kalshi:
    base_url: "{KALSHI}"
    request_delay_seconds: 0
mapping_review:
  max_mid_diff: 0.10
  max_kalshi_spread: 0.05
  min_kalshi_volume: 1000
"""

SIG_PRICES = {  # exchange id -> (bestBid, bestAsk)
    "1901": (0.60, 0.64),
    "1902": (0.36, 0.40),
    "1911": (0.60, 0.64),
    "1921": (0.55, 0.60),
    "1931": (0.50, 0.55),
}


def kalshi_market(ticker: str, event: str, title: str, yes: str, bid: str, ask: str, volume: str = "50000.00") -> dict:
    return {
        "ticker": ticker,
        "event_ticker": event,
        "market_type": "binary",
        "title": title,
        "subtitle": "",
        "yes_sub_title": yes,
        "no_sub_title": yes,
        "status": "active",
        "close_time": "2026-11-04T05:00:00Z",
        "expected_expiration_time": "2026-11-04T15:00:00Z",
        "latest_expiration_time": "2027-11-04T15:00:00Z",
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "last_price_dollars": bid,
        "volume_fp": volume,
        "volume_24h_fp": "100.00",
        "rules_primary": f"If {yes} wins the 2026 general election, the market resolves to Yes.",
        "rules_secondary": "",
    }


KALSHI_MARKETS = {
    "SENATEAZ-26-D": kalshi_market("SENATEAZ-26-D", "SENATEAZ-26", "Which party will win the Arizona Senate race?", "Democratic party", "0.6100", "0.6300"),
    "SENATEAZ-26-R": kalshi_market("SENATEAZ-26-R", "SENATEAZ-26", "Which party will win the Arizona Senate race?", "Republican party", "0.3700", "0.3900"),
    "SENATECO-26-R": kalshi_market("SENATECO-26-R", "SENATECO-26", "Which party will win the Colorado Senate race?", "Republican party", "0.3700", "0.3900"),
    "GOVPRIMARYCT-26-D": kalshi_market("GOVPRIMARYCT-26-D", "GOVPRIMARYCT-26", "Who will win the Connecticut Democratic primary for Governor?", "Jane Doe", "0.5600", "0.5900"),
}  # fmt: skip


EVENT_TITLES = {
    "SENATEAZ-26": "Arizona Senate winner?",
    "SENATECO-26": "Colorado Senate winner?",
    "GOVPRIMARYCT-26": "Connecticut Democratic Governor primary",
}


def kalshi_event(ticker: str) -> dict:
    return {
        "event_ticker": ticker,
        "series_ticker": ticker.split("-")[0],
        "title": EVENT_TITLES[ticker],
        "sub_title": "",
        "collateral_return_type": "MECNET",
        "mutually_exclusive": True,
        "settlement_sources": [{"name": "AP", "url": "https://apnews.com"}],
    }


def handler(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET", f"show_mapping must be read-only, got {request.method} {request.url}"
    url = str(request.url)
    path = request.url.path
    if url.startswith(SIG):
        assert request.headers["Authorization"] == "Bearer test-key"
        if path.endswith("/tournaments/test-cup"):
            return httpx.Response(200, json={"id": TOURNAMENT_ID, "slug": "test-cup"})
        assert request.url.params.get("tournamentId") == TOURNAMENT_ID, f"missing Cup tournamentId: {url}"
        parts = path.split("/")
        if "/markets/" in path:
            return httpx.Response(200, json={"id": parts[-1], "title": "x", "status": "open", "settlementDate": None})
        if path.endswith("/price"):
            bid, ask = SIG_PRICES[parts[-2]]
            return httpx.Response(
                200,
                json={"exchangeId": parts[-2], "marketId": "", "option": "YES", "latestPrice": None,
                      "bestBid": bid, "bestAsk": ask, "spread": ask - bid},
            )  # fmt: skip
    if url.startswith(KALSHI):
        if path.endswith("/markets") and "tickers" in request.url.params:  # batched read
            wanted = request.url.params["tickers"].split(",")
            return httpx.Response(200, json={"markets": [KALSHI_MARKETS[t] for t in wanted if t in KALSHI_MARKETS],
                                             "cursor": ""})  # fmt: skip
        ticker = path.split("/")[-1]
        if "/markets/" in path and ticker in KALSHI_MARKETS:
            return httpx.Response(200, json={"market": KALSHI_MARKETS[ticker]})
        if "/events/" in path:
            return httpx.Response(200, json={"event": kalshi_event(ticker), "markets": []})
    return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.setenv("SIG_API_KEY", "test-key")
    paths = {"map": tmp_path / "market_map.csv", "markets": tmp_path / "cup.csv", "settings": tmp_path / "s.yaml"}
    paths["map"].write_text(MAP_CSV)
    paths["markets"].write_text(CUP_CSV)
    paths["settings"].write_text(SETTINGS)
    return paths


def run(files: dict[str, Path], argv: list[str], answer: str = "") -> tuple[int, str]:
    lines: list[str] = []
    prompts: list[str] = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return answer

    code = show_mapping.main(
        argv,
        transport=httpx.MockTransport(handler),
        input_fn=fake_input,
        out=lines.append,
        map_path=files["map"],
        markets_path=files["markets"],
        settings_path=files["settings"],
    )
    return code, "\n".join(lines + prompts)


def test_correct_race_prints_both_sides_and_no_warnings(files):
    code, text = run(files, ["AZ-Senate"])
    assert code == 0
    assert "SIG 'Will the Democratic Party win the Arizona Senate?' YES = Kalshi YES on SENATEAZ-26-D ('Democratic party')" in text
    assert "market id 901  exchange id 1901  party Democratic" in text
    assert "Cup book: bid 0.600  ask 0.640" in text
    assert "bid 0.610  ask 0.630" in text
    assert "rules: NONE - the SIG markets API returned no rules/description/settlement text" in text
    assert "If Democratic party wins the 2026 general election" in text
    assert "notes, with a comma" in text
    assert "WARNING" not in text
    assert "No warnings in AZ-Senate." in text


def test_effectively_inverted_polarity_warns(files):
    code, text = run(files, ["CO-Senate"])
    assert code == 0
    assert "WARNING: SIG mid 0.620 vs Kalshi mid 0.380 (after polarity) differ by 24.0 points" in text


def test_primary_contract_warns(files):
    _, text = run(files, ["CT-Governor"])
    assert "WARNING: looks like a PRIMARY/nominee contract" in text


def test_unmapped_race_warns(files):
    _, text = run(files, ["DE-Senate"])
    assert "WARNING: no Kalshi ticker mapped" in text


def test_race_key_is_case_insensitive_and_unknown_suggests(files):
    assert run(files, ["az-senate"])[0] == 0
    code, text = run(files, ["AZ-Senat"])
    assert code == 2 and "AZ-Senate" in text


def test_list_shows_tier_verified_confidence(files):
    code, text = run(files, ["--list"])
    assert code == 0
    assert text.splitlines()[1].split() == ["AZ-Senate", "2", "-", "false", "0.90", "2/2"]
    assert "DE-Senate" in text and "0/1" in text


def test_mark_verified_changes_only_target_rows(files):
    before = files["map"].read_bytes().decode().splitlines(keepends=True)
    code, text = run(files, ["AZ-Senate", "--mark-verified"], answer="yes")
    assert code == 0
    after = files["map"].read_bytes().decode().splitlines(keepends=True)
    assert len(after) == len(before)
    for old, new in zip(before, after):
        if old.startswith(("901,", "902,")):
            assert new.rstrip("\n").endswith(",true,A")
            assert new.split(",")[:4] == old.split(",")[:4]  # ticker, polarity untouched
        else:
            assert new == old
    assert 'notes, with a comma' in after[2]


def test_mark_verified_needs_exact_yes(files):
    before = files["map"].read_bytes()
    code, _ = run(files, ["AZ-Senate", "--mark-verified"], answer="y")
    assert code == 1
    assert files["map"].read_bytes() == before


def test_mark_verified_refuses_race_without_kalshi_ticker(files):
    before = files["map"].read_bytes()
    code, text = run(files, ["DE-Senate", "--mark-verified"], answer="yes")
    assert code == 1 and "Refusing" in text
    assert files["map"].read_bytes() == before


# --- pure warning logic -----------------------------------------------------


def _km(**overrides) -> dict:
    raw = kalshi_market("SENATEAZ-26-R", "SENATEAZ-26", "Which party will win the Arizona Senate race?", "Republican party", "0.3700", "0.3900")
    raw.update(overrides)
    return raw


def test_correct_inverted_polarity_does_not_warn():
    # SIG D at 0.62 vs Kalshi R at 0.38, mapped as inverted: consistent.
    w = review_warnings(sig_bid=0.60, sig_ask=0.64, polarity="inverted", ticker="SENATEAZ-26-R",
                        sig_state="AZ", kalshi=parse_market(_km()), event=None, th=TH)  # fmt: skip
    assert w == []


def test_non_2026_contest_warns():
    raw = _km(title="Which party will win the Arizona Senate race in 2028?", expected_expiration_time="2028-11-08T15:00:00Z")
    w = review_warnings(sig_bid=0.36, sig_ask=0.40, polarity="same", ticker="X", sig_state="AZ", kalshi=parse_market(raw), event=None, th=TH)
    assert any("2028" in x and "not 2026" in x for x in w)
    assert any("outside the 2026 cycle" in x for x in w)


def test_wide_spread_and_low_volume_warn():
    raw = _km(yes_bid_dollars="0.3000", yes_ask_dollars="0.4000", volume_fp="12.00")
    w = review_warnings(sig_bid=0.33, sig_ask=0.37, polarity="same", ticker="X", sig_state="AZ", kalshi=parse_market(raw), event=None, th=TH)
    assert any("spread 10.0 points" in x for x in w)
    assert any("volume very low" in x for x in w)


def test_empty_kalshi_side_is_not_a_price():
    m = parse_market(_km(yes_bid_dollars="0.0000"))
    assert m.yes_bid is None and m.mid is None


# --- state check on the real SENATELA-26 event (titled "Kentucky Senate winner?")


def _real_senatela() -> tuple:
    import json

    from predcup.venues.kalshi import parse_event

    fixture = Path(__file__).parent / "fixtures" / "kalshi_senate_events_2026-09-30.json"
    ev = next(e for e in json.loads(fixture.read_text())["SENATELA"] if e["event_ticker"] == "SENATELA-26")
    market = next(m for m in ev["markets"] if m["ticker"] == "SENATELA-26-R")
    raw = _km(**{k: market[k] for k in ("ticker", "event_ticker", "title", "subtitle", "yes_sub_title", "rules_primary")})
    return parse_market(raw), parse_event(ev)


def test_senatela_mapped_to_louisiana_warns_state_mismatch():
    kalshi, event = _real_senatela()
    w = review_warnings(sig_state="LA", sig_bid=0.36, sig_ask=0.40, polarity="same", ticker=kalshi.ticker,
                        kalshi=kalshi, event=event, th=TH)  # fmt: skip
    assert any("STATE MISMATCH" in x and "is KY, SIG race is LA" in x for x in w)


def test_senatela_mapped_to_kentucky_has_no_state_warning():
    kalshi, event = _real_senatela()
    w = review_warnings(sig_state="KY", sig_bid=0.36, sig_ask=0.40, polarity="same", ticker=kalshi.ticker,
                        kalshi=kalshi, event=event, th=TH)  # fmt: skip
    assert not any("state" in x.lower() for x in w)


def test_chamber_control_race_skips_state_check():
    kalshi, event = _real_senatela()
    w = review_warnings(sig_state="US", sig_bid=0.36, sig_ask=0.40, polarity="same", ticker=kalshi.ticker,
                        kalshi=kalshi, event=event, th=TH)  # fmt: skip
    assert not any("state" in x.lower() for x in w)


# --- --summary: one line per SIG market ---------------------------------------

SUMMARY_CUP_CSV = CUP_CSV + "912,1912,Will the Republican Party win the Colorado Senate?,Election Outcome,CO,Senate,,R,CO-Senate\n"

# AZ D/R and CO R verified and consistent; CO D (911) is unverified and points
# at Kalshi's Republican contract, so its party ID R disagrees with the D
# that every other verified D row (901) uses.
SUMMARY_MAP_CSV = """platform_id,kalshi_ticker,poly_token_id,polarity,rule_diff_notes,confidence,verified,tier
901,SENATEAZ-26-D,,same,,0.9,true,A
902,SENATEAZ-26-R,,same,,0.9,true,A
911,SENATECO-26-R,,same,,0.9,false,
912,SENATECO-26-R,,same,,0.9,true,A
921,GOVPRIMARYCT-26-D,,same,,0.9,false,
931,,,,no Kalshi series,0.0,false,
"""


@pytest.fixture
def summary_files(files, monkeypatch):
    files["map"].write_text(SUMMARY_MAP_CSV)
    files["markets"].write_text(SUMMARY_CUP_CSV)
    monkeypatch.setitem(SIG_PRICES, "1912", (0.36, 0.40))
    return files


def _summary_line(text: str, market_id: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.split()[:1] == [market_id]]
    assert len(lines) == 1, text
    return lines[0]


def test_summary_one_line_per_market_with_prices_and_gap(summary_files):
    before = summary_files["map"].read_bytes()
    code, text = run(summary_files, ["--summary", "AZ-Senate", "CO-Senate"])
    assert code == 0
    line = _summary_line(text, "901")
    assert line.split()[:4] == ["901", "AZ-Senate", "D", "SENATEAZ-26-D"]
    assert "Democratic party" in line  # Kalshi YES label = candidate
    assert line.split("Democratic party")[1].split()[:2] == ["D", "-"]  # party ID, no other verified D row
    assert "SIG 0.600/0.640" in line
    assert "K 0.610/0.630" in line
    assert "gap +0.0" in line  # 0.620 - 0.620
    assert summary_files["map"].read_bytes() == before  # --summary never writes


def test_summary_party_id_matches_other_verified_rows(summary_files):
    _, text = run(summary_files, ["--summary", "AZ-Senate", "CO-Senate"])
    assert " ok " in _summary_line(text, "902")  # 912 also uses R for R
    assert " ok " in _summary_line(text, "912")
    assert " - " in _summary_line(text, "901")  # no other verified D row


def test_summary_flags_party_id_mismatch_and_gap_warning(summary_files):
    _, text = run(summary_files, ["--summary", "CO-Senate"])
    line = _summary_line(text, "911")
    assert "MISMATCH(D)" in line
    assert "gap +24.0" in line
    assert "differ by 24.0 points" in line
    assert "Kalshi party ID R, other verified D rows use D" in line


def test_summary_unmapped_market_still_gets_a_line(summary_files):
    code, text = run(summary_files, ["--summary", "DE-Senate"])
    assert code == 0
    line = _summary_line(text, "931")
    assert "no Kalshi ticker mapped" in line


def test_summary_unknown_race_fails_before_any_read(summary_files):
    code, text = run(summary_files, ["--summary", "AZ-Senate", "XX-Nothing"])
    assert code == 2 and "XX-Nothing" in text


def test_summary_needs_a_race(summary_files):
    with pytest.raises(SystemExit):
        run(summary_files, ["--summary"])


# --- pure party-ID logic ------------------------------------------------------

from predcup.mapping_review import kalshi_party_id, party_id_consensus  # noqa: E402


def test_kalshi_party_id_is_the_ticker_suffix():
    assert kalshi_party_id("SENATEAZ-26-D") == "D"
    assert kalshi_party_id("GOVPARTYRI-26-R") == "R"
    assert kalshi_party_id("KXSENATELA-26NOV-JSMI") == "JSMI"
    assert kalshi_party_id("") is None


def test_party_id_consensus_uses_only_other_verified_rows_of_that_party_and_polarity():
    party_of = {"1": "D", "2": "D", "3": "D", "4": "R", "5": "D"}
    rows = [
        {"platform_id": "1", "kalshi_ticker": "A-26-D", "polarity": "same", "verified": "true"},
        {"platform_id": "2", "kalshi_ticker": "B-26-D", "polarity": "same", "verified": "TRUE"},
        {"platform_id": "3", "kalshi_ticker": "C-26-R", "polarity": "same", "verified": "false"},
        {"platform_id": "4", "kalshi_ticker": "D-26-R", "polarity": "same", "verified": "true"},
        {"platform_id": "5", "kalshi_ticker": "E-26-R", "polarity": "inverted", "verified": "true"},
    ]
    assert party_id_consensus(rows, party_of, party="D", polarity="same", exclude_id="1") == {"D"}
    assert party_id_consensus(rows, party_of, party="D", polarity="same", exclude_id="3") == {"D"}
    assert party_id_consensus(rows, party_of, party="R", polarity="same", exclude_id="4") == set()
    assert party_id_consensus(rows, party_of, party="D", polarity="inverted", exclude_id="1") == {"R"}


# --- --summary: Kalshi custom_strike political_party ID (2026-10-01) ------------------
#
# The suffix check (-D / -R) reads the ticker; the political_party UUID in
# the Kalshi market's custom_strike is what its rules resolve on. A row can
# pass the suffix check and still point at the other party's UUID.

from predcup.mapping_review import kalshi_political_party, political_party_consensus  # noqa: E402
from predcup.venues.kalshi import parse_market as _parse  # noqa: E402

DEM, REP = "57fa2293-3102-463b-9087-68cd9f6da0a6", "9244ed4c-9dfd-45cc-8211-996dc902f315"


def test_kalshi_political_party_reads_custom_strike():
    m = _parse({**kalshi_market("X-26-D", "X-26", "t", "Jane", "0.40", "0.42"),
                "custom_strike": {"political_party": DEM, "politician": "p"}})  # fmt: skip
    assert kalshi_political_party(m) == DEM
    assert kalshi_political_party(_parse(kalshi_market("X-26-I", "X-26", "t", "Ind", "0.1", "0.12"))) is None
    assert kalshi_political_party(None) is None


def test_political_party_consensus_uses_other_verified_rows_of_that_party():
    rows = [
        {"platform_id": "1", "kalshi_ticker": "A-26-D", "polarity": "same", "verified": "true"},
        {"platform_id": "2", "kalshi_ticker": "B-26-D", "polarity": "same", "verified": "true"},
        {"platform_id": "3", "kalshi_ticker": "C-26-D", "polarity": "same", "verified": "false"},
        {"platform_id": "4", "kalshi_ticker": "D-26-R", "polarity": "same", "verified": "true"},
    ]
    party_of = {"1": "D", "2": "D", "3": "D", "4": "R"}
    uuid_of = {"A-26-D": DEM, "B-26-D": DEM, "C-26-D": REP, "D-26-R": REP}
    assert political_party_consensus(rows, party_of, uuid_of, party="D", polarity="same", exclude_id="3") == {DEM}
    assert political_party_consensus(rows, party_of, uuid_of, party="R", polarity="same", exclude_id="4") == set()


@pytest.fixture
def party_files(summary_files, monkeypatch):
    # AZ D/R and CO R verified with the real Kalshi UUIDs; DE D (932) is
    # unverified, its ticker ends in -D (suffix check passes) but its
    # custom_strike carries the Republican UUID.
    summary_files["markets"].write_text(SUMMARY_CUP_CSV
        + "932,1932,Will the Democratic Party win the Delaware Senate?,Election Outcome,DE,Senate,,D,DE-Senate\n")  # fmt: skip
    summary_files["map"].write_text(SUMMARY_MAP_CSV + "932,SENATEDE-26-D,,same,,0.9,false,\n")
    monkeypatch.setitem(SIG_PRICES, "1932", (0.60, 0.64))
    for ticker, uuid in (("SENATEAZ-26-D", DEM), ("SENATEAZ-26-R", REP), ("SENATECO-26-R", REP)):
        monkeypatch.setitem(KALSHI_MARKETS, ticker, {**KALSHI_MARKETS[ticker], "custom_strike": {"political_party": uuid}})
    monkeypatch.setitem(KALSHI_MARKETS, "SENATEDE-26-D", {
        **kalshi_market("SENATEDE-26-D", "SENATEDE-26", "Will Democratics win the Senate race in Delaware?",
                        "Democratic party", "0.6100", "0.6300"),
        "custom_strike": {"political_party": REP}})  # fmt: skip
    monkeypatch.setitem(EVENT_TITLES, "SENATEDE-26", "Delaware Senate winner?")
    return summary_files


def test_summary_political_party_ok_against_other_verified_rows(party_files):
    _, text = run(party_files, ["--summary", "AZ-Senate", "CO-Senate"])
    assert f"{REP[:8]} ok" in _summary_line(text, "902")  # 912 also uses REP for R
    assert f"{DEM[:8]} -" in _summary_line(text, "901")  # no other verified D row


def test_summary_flags_political_party_mismatch_the_suffix_check_misses(party_files):
    _, text = run(party_files, ["--summary", "DE-Senate"])
    line = _summary_line(text, "932")
    assert " D      ok " in line  # suffix check passes
    assert f"{REP[:8]} MISMATCH({DEM[:8]})" in line
    assert f"Kalshi political_party {REP[:8]}, other verified D rows use {DEM[:8]}" in line
