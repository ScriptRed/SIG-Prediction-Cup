"""With fair_value.use_polymarket off, fair values are byte-identical to
the Kalshi-only code. The golden file was written by that code (commit
23a8db0, before the blend existed) over tests/fairvalue_golden.py's grid
of 4335 cases; here every case is replayed through today's entry point,
with each kind of Polymarket quote present, and compared as exact repr
strings (full float precision, every field)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from predcup.fairvalue import fair_value, kalshi_fair_value, load_fair_value_config
from tests.fairvalue_golden import GOLDEN, NOW, cases, settings
from tests.test_fairvalue_polymarket import PCFG, TOKEN, pquote

GOLDEN_CASES = json.loads(GOLDEN.read_text())
POLY_QUOTES = {
    "none": None,
    "agreeing": pquote(bid="0.6000", ask="0.6200"),
    "disagreeing": pquote(bid="0.9000", ask="0.9200"),
    "stale": pquote(age_s=600),
    "one-sided": pquote(ask=""),
}


def _off_configs():
    shipped = load_fair_value_config(settings())  # config/settings.yaml as committed
    assert shipped.use_polymarket is False
    yield "shipped", shipped
    yield "off-with-poly-block", replace(shipped, polymarket=PCFG)
    yield "off-without-poly-block", replace(shipped, polymarket=None)


def test_golden_covers_both_outcomes():
    assert len(GOLDEN_CASES) == 4335
    assert sum("ok=True" in v for v in GOLDEN_CASES.values()) == 320


def test_kalshi_fair_value_is_unchanged():
    cfg = load_fair_value_config(settings())
    got = {cid: repr(kalshi_fair_value(row, q, NOW, cfg)) for cid, row, q in cases()}
    assert got == GOLDEN_CASES


@pytest.mark.parametrize("cfg_name,cfg", list(_off_configs()))
@pytest.mark.parametrize("poly_name", list(POLY_QUOTES))
def test_fair_value_with_polymarket_off_is_byte_identical(cfg_name, cfg, poly_name):
    pq = POLY_QUOTES[poly_name]
    for cid, row, q in cases():
        row = {**row, "poly_token_id": TOKEN} if row.get("poly_token_id") else row
        assert repr(fair_value(row, q, pq, NOW, cfg)) == GOLDEN_CASES[cid], cid


def test_the_blend_would_change_something_when_on():
    # guards the test above against a vacuous pass
    cfg = replace(load_fair_value_config(settings()), use_polymarket=True, polymarket=PCFG)
    pq = POLY_QUOTES["agreeing"]
    changed = sum(
        repr(fair_value({**row, "poly_token_id": TOKEN}, q, pq, NOW, cfg)) != GOLDEN_CASES[cid] for cid, row, q in cases()
    )
    assert changed > 0
