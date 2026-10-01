"""Input grid for the fair-value golden file (tests/fixtures/
fairvalue_kalshi_golden_2026-10-01.json). The golden file was written by
the Kalshi-only fairvalue.py at commit 23a8db0, before the Polymarket blend
existed; test_fairvalue_polymarket_off.py replays this grid through today's
code with use_polymarket off and compares bytes.

    python -m tests.fairvalue_golden   # regenerate (only if the grid changes)
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from predcup.venues.kalshi import parse_market

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/fixtures/fairvalue_kalshi_golden_2026-10-01.json"
NOW = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)

BASE_ROW = {"platform_id": "379", "kalshi_ticker": "SENATEMA-26-D", "poly_token_id": "123", "polarity": "same",
            "confidence": "0.8", "verified": "true", "tier": "A", "fusion_risk": "false"}  # fmt: skip
ROW_OVERRIDES = [
    {}, {"polarity": "inverted"}, {"verified": "false"}, {"verified": ""}, {"tier": "B"}, {"tier": ""},
    {"kalshi_ticker": ""}, {"polarity": ""}, {"polarity": "flipped"}, {"confidence": "0.3"},
    {"confidence": "0.5"}, {"confidence": "x"}, {"confidence": ""}, {"verified": "false", "confidence": "1.0"},
    {"poly_token_id": ""},
]  # fmt: skip
BOOKS = [
    ("0.6000", "0.6200"), ("0.0000", "0.6200"), ("0.6000", "1.0000"), ("0.0000", "1.0000"), ("0.4000", "0.5500"),
    ("0.6300", "0.6100"), ("0.0100", "0.0300"), ("0.9700", "0.9900"), ("0.4500", "0.5500"), ("0.3333", "0.3367"),
    (None, "0.5000"), ("0.5000", None),
]  # fmt: skip
VOLUMES = ["50000.00", "999.99", "1000.00", "0"]
AGES = [0.0, 5.0, 59.9, 60.0, 61.0, -3.0]


def settings() -> dict:
    return yaml.safe_load((ROOT / "config/settings.yaml").read_text())


def kalshi_quote(bid, ask, volume, age):
    from predcup.fairvalue import KalshiQuote

    raw = {"ticker": "SENATEMA-26-D", "event_ticker": "SENATEMA-26", "title": "t", "subtitle": "",
           "yes_sub_title": "", "no_sub_title": "", "status": "active", "yes_bid_dollars": bid,
           "yes_ask_dollars": ask, "volume_fp": volume, "rules_primary": "", "rules_secondary": ""}  # fmt: skip
    return KalshiQuote(market=parse_market(raw), fetched_at=NOW - timedelta(seconds=age))


def cases():
    """(case id, map_row, kalshi quote or None)."""
    for i, o in enumerate(ROW_OVERRIDES):
        row = {**BASE_ROW, **o}
        yield f"row{i}-noquote", row, None
        for (bid, ask), vol, age in itertools.product(BOOKS, VOLUMES, AGES):
            yield f"row{i}-{bid}-{ask}-{vol}-{age}", row, kalshi_quote(bid, ask, vol, age)


def main() -> None:
    from predcup.fairvalue import kalshi_fair_value, load_fair_value_config

    cfg = load_fair_value_config(settings())
    out = {cid: repr(kalshi_fair_value(row, q, NOW, cfg)) for cid, row, q in cases()}
    GOLDEN.write_text(json.dumps(out, indent=0, sort_keys=True) + "\n")
    print(f"wrote {len(out)} cases to {GOLDEN}")


if __name__ == "__main__":
    main()
