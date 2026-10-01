"""Optional Kalshi + Polymarket blend (fair_value.use_polymarket). Kalshi
stays the anchor: every v1 gate (verified, Tier A, confidence, fresh
two-sided Kalshi book) still applies, and Polymarket alone never yields a
fair value. Blend = liquidity-weighted log-odds average, weight 1/u^2 per
venue (u = that venue's half-spread + base + staleness + thin-book
penalty); uncertainty widens with the venues' disagreement."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta

import pytest

from predcup.fairvalue import (
    PolymarketFairValueConfig,
    PolymarketQuote,
    fair_value,
    kalshi_fair_value,
    load_fair_value_config,
)
from predcup.venues.polymarket import parse_book
from tests.test_fairvalue import CFG, NOW, ROW, quote

TOKEN = "21265207456609426291246075480390336499088453711419597084147957999650569091884"
PCFG = PolymarketFairValueConfig(
    max_spread=0.10,
    base_uncertainty=0.005,
    thin_depth=500,
    thin_penalty=0.01,
    stale_penalty=0.01,
    disagreement_factor=0.5,
    max_disagreement=0.08,
)
ON = replace(CFG, use_polymarket=True, polymarket=PCFG)
PROW = {**ROW, "poly_token_id": TOKEN}


def pquote(bid="0.6200", ask="0.6400", bid_size="1000", ask_size="1000", age_s=5.0, token=TOKEN) -> PolymarketQuote:
    raw = {"market": "0xabc", "asset_id": token, "timestamp": "1790815048261", "hash": "h",
           "bids": [{"price": bid, "size": bid_size}] if bid else [],
           "asks": [{"price": ask, "size": ask_size}] if ask else [],
           "min_order_size": "5", "tick_size": "0.01", "neg_risk": True, "last_trade_price": "0.63"}  # fmt: skip
    return PolymarketQuote(book=parse_book(raw), fetched_at=NOW - timedelta(seconds=age_s))


def blend(row=PROW, kq=None, pq=None, cfg=ON):
    return fair_value(row, kq if kq is not None else quote(), pq if pq is not None else pquote(), NOW, cfg)


def logit(p):
    return math.log(p / (1 - p))


def expit(x):
    return 1 / (1 + math.exp(-x))


# --- the blend ------------------------------------------------------------------


def test_blend_is_inverse_uncertainty_squared_weighted_log_odds_average():
    k = kalshi_fair_value(PROW, quote(), NOW, CFG)  # 0.61, u_k
    u_p = 0.01 + 0.005 + 0.01 * 5 / 60  # half-spread + base + staleness
    w_k, w_p = 1 / k.uncertainty**2, 1 / u_p**2
    expected = expit((w_k * logit(0.61) + w_p * logit(0.63)) / (w_k + w_p))
    r = blend()
    assert r.ok and r.source == "kalshi+polymarket"
    assert r.value == pytest.approx(expected)
    base_u = (w_k * k.uncertainty + w_p * u_p) / (w_k + w_p)
    assert r.uncertainty == pytest.approx(base_u + 0.5 * 0.02)


def test_equal_books_give_the_log_odds_midpoint():
    r = blend(kq=quote(bid="0.0500", ask="0.0700"), pq=pquote(bid="0.1100", ask="0.1300"))
    # same spread and age; neither book thin (Kalshi volume 50k, Poly depth 2000)
    assert r.value == pytest.approx(expit((logit(0.06) + logit(0.12)) / 2))
    assert r.value < (0.06 + 0.12) / 2  # log-odds, not a plain average


def test_the_tighter_book_gets_more_weight():
    tight_poly = blend(pq=pquote(bid="0.6600", ask="0.6620"))
    wide_poly = blend(pq=pquote(bid="0.6200", ask="0.7000"))
    assert abs(tight_poly.value - 0.661) < abs(wide_poly.value - 0.66)


def test_thin_polymarket_book_gets_less_weight():
    deep = blend(pq=pquote(bid_size="1000", ask_size="1000"))
    thin = blend(pq=pquote(bid_size="10", ask_size="10"))
    assert abs(thin.value - 0.61) < abs(deep.value - 0.61)


def test_uncertainty_widens_with_disagreement():
    near = blend(pq=pquote(bid="0.6000", ask="0.6200"))  # same mid as Kalshi
    far = blend(pq=pquote(bid="0.6600", ask="0.6800"))  # 6 points away
    assert far.uncertainty > near.uncertainty
    assert far.uncertainty - near.uncertainty == pytest.approx(0.5 * 0.06, abs=1e-3)


def test_too_much_disagreement_gives_no_fair_value():
    r = blend(pq=pquote(bid="0.7000", ask="0.7200"))  # 10 points from Kalshi
    assert not r.ok and r.value is None and "disagree" in r.reason


def test_as_of_is_the_older_of_the_two_quotes():
    r = blend(kq=quote(age_s=5), pq=pquote(age_s=20))
    assert r.as_of == NOW - timedelta(seconds=20)


def test_non_same_polarity_is_not_blended():
    # the matcher maps the same party on Polymarket, but the one polarity column was set for Kalshi
    r = blend(row={**PROW, "polarity": "inverted"}, kq=quote(bid="0.3800", ask="0.4000"))
    assert r.ok and r.source == "kalshi"  # not blended: polarity column is ambiguous for Polymarket


# --- Kalshi stays the anchor ------------------------------------------------------


@pytest.mark.parametrize(
    "row,kq",
    [({**PROW, "verified": "false"}, None), ({**PROW, "tier": "B"}, None), ({**PROW, "confidence": "0.1"}, None),
     (PROW, "stale"), (PROW, "one-sided")],
)  # fmt: skip
def test_no_kalshi_fair_value_means_no_fair_value_even_with_a_good_polymarket_book(row, kq):
    kquote = {None: quote(), "stale": quote(age_s=61), "one-sided": quote(bid="0.0000")}[kq]
    r = fair_value(row, kquote, pquote(), NOW, ON)
    assert not r.ok and r == kalshi_fair_value(row, kquote, NOW, CFG)


def test_no_kalshi_quote_at_all_means_no_fair_value():
    assert not fair_value(PROW, None, pquote(), NOW, ON).ok


# --- unusable Polymarket -> Kalshi-only, unchanged -----------------------------------


@pytest.mark.parametrize(
    "row,pq",
    [({**PROW, "poly_token_id": ""}, pquote()),
     (PROW, None),
     (PROW, pquote(age_s=61)),
     (PROW, pquote(ask="")),
     (PROW, pquote(bid="")),
     (PROW, pquote(bid="0.6500", ask="0.6300")),
     (PROW, pquote(bid="0.5000", ask="0.6500")),
     (PROW, pquote(token="999"))],
    ids=["no-token", "no-quote", "stale", "no-ask", "no-bid", "crossed", "wide", "wrong-token"],
)  # fmt: skip
def test_unusable_polymarket_falls_back_to_exactly_the_kalshi_value(row, pq):
    r = fair_value(row, quote(), pq, NOW, ON)
    assert r == kalshi_fair_value(row, quote(), NOW, CFG)


# --- config --------------------------------------------------------------------------


SETTINGS = {"fair_value": {"max_outside_data_age_seconds": 60, "min_confidence_to_trade": 0.5,
            "kalshi": {"max_spread": 0.1, "base_uncertainty": 0.005, "thin_volume": 1000,
                       "thin_penalty": 0.01, "stale_penalty": 0.01}}}  # fmt: skip
PSET = {"max_spread": 0.10, "base_uncertainty": 0.005, "thin_depth": 500, "thin_penalty": 0.01,
        "stale_penalty": 0.01, "disagreement_factor": 0.5, "max_disagreement": 0.08}  # fmt: skip


def test_config_defaults_to_off():
    cfg = load_fair_value_config(SETTINGS)
    assert cfg.use_polymarket is False and cfg.polymarket is None


def test_config_loads_polymarket_block():
    cfg = load_fair_value_config({"fair_value": {**SETTINGS["fair_value"], "use_polymarket": True, "polymarket": PSET}})
    assert cfg == ON


def test_turning_it_on_without_a_polymarket_block_refuses():
    with pytest.raises(ValueError, match="polymarket"):
        load_fair_value_config({"fair_value": {**SETTINGS["fair_value"], "use_polymarket": True}})


@pytest.mark.parametrize("value", ["false", "yes", 1, None])
def test_use_polymarket_must_be_a_real_boolean(value):
    with pytest.raises(ValueError, match="use_polymarket"):
        load_fair_value_config({"fair_value": {**SETTINGS["fair_value"], "use_polymarket": value, "polymarket": PSET}})


def test_repo_settings_ship_with_it_off():
    import yaml

    from tests.fairvalue_golden import settings

    cfg = load_fair_value_config(settings())
    assert cfg.use_polymarket is False
    assert yaml.safe_load(open("config/settings.yaml"))["fair_value"]["use_polymarket"] is False
