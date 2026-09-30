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
