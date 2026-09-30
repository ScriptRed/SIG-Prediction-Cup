"""strategies/quoter.py v1: simplified Avellaneda-Stoikov around the Kalshi
fair value, sized small, many markets per cycle, one cancel-all plus
batched re-post, short expiry on every quote."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from predcup.control import TradingControl
from predcup.fairvalue import FairValue
from predcup.strategies.quoter import (
    QuoterConfig,
    QuoteTarget,
    Quoter,
    compute_quote,
    in_blackout,
    load_quoter_config,
)

TID = "550e8400-e29b-41d4-a716-446655440000"
NOW = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)
CFG = QuoterConfig(
    min_edge=0.01,
    uncertainty_coefficient=1.0,
    inventory_skew_coefficient=0.02,
    requote_move=0.01,
    quote_size=20,
    expiration_seconds=30,
    refresh_interval_seconds=20,
    max_markets=30,
    max_position_shares=200,
    max_price_deviation=0.03,
    post_only=True,
    blackouts=(),
)


def fv(value=0.5, unc=0.01, age=5):
    return FairValue(ok=True, value=value, uncertainty=unc, as_of=NOW - timedelta(seconds=age))


# --- compute_quote (pure) -------------------------------------------------------


def test_symmetric_quote_around_fair_value():
    q = compute_quote(fv(0.5, 0.01), position=0, best_bid=None, best_ask=None, cfg=CFG)
    assert (q.bid, q.ask) == (0.49, 0.51)


def test_half_spread_is_max_of_min_edge_and_c_times_uncertainty():
    q = compute_quote(fv(0.5, 0.02), 0, None, None, CFG)
    assert (q.bid, q.ask) == (0.48, 0.52)
    q = compute_quote(fv(0.5, 0.002), 0, None, None, CFG)
    assert (q.bid, q.ask) == (0.49, 0.51)


def test_prices_rounded_away_from_fair_value_to_tick():
    q = compute_quote(fv(0.5023, 0.01), 0, None, None, CFG)
    assert (q.bid, q.ask) == (0.49, 0.515)


def test_long_inventory_skews_quotes_down():
    q = compute_quote(fv(0.5, 0.01), position=100, best_bid=None, best_ask=None, cfg=CFG)  # half of max -> -0.01
    assert (q.bid, q.ask) == (0.48, 0.5)


def test_at_max_position_stop_adding():
    assert compute_quote(fv(), position=200, best_bid=None, best_ask=None, cfg=CFG).bid is None
    assert compute_quote(fv(), position=-200, best_bid=None, best_ask=None, cfg=CFG).ask is None


def test_no_quote_without_fair_value():
    q = compute_quote(FairValue.none("stale"), 0, None, None, CFG)
    assert q.bid is None and q.ask is None and "stale" in q.reason


def test_uncertainty_beyond_risk_band_means_no_quote():
    q = compute_quote(fv(0.5, 0.05), 0, None, None, CFG)
    assert q.bid is None and q.ask is None and "uncertainty" in q.reason


def test_post_only_never_crosses_the_sig_book():
    # Fair value 0.5 but SIG ask at 0.47: a 0.49 bid would take; back off to 0.465.
    q = compute_quote(fv(0.5, 0.01), 0, best_bid=0.40, best_ask=0.47, cfg=CFG)
    assert q.bid == 0.465 and q.ask == 0.51


def test_sides_outside_price_range_are_dropped():
    q = compute_quote(fv(0.01, 0.01), 0, None, None, CFG)
    assert q.bid is None and q.ask == 0.02
    q = compute_quote(fv(0.995, 0.01), 0, None, None, CFG)
    assert q.ask is None and q.bid == 0.985


def test_blackout_windows():
    windows = ((NOW - timedelta(minutes=5), NOW + timedelta(minutes=5)),)
    assert in_blackout(NOW, windows)
    assert not in_blackout(NOW + timedelta(minutes=6), windows)


def test_config_from_settings_and_expiry_must_outlast_refresh():
    s = {
        "quoter": {"min_edge": 0.01, "uncertainty_coefficient": 1.0, "inventory_skew_coefficient": 0.02,
                   "requote_fair_value_move_ticks": 2, "quote_expiration_seconds": 30,
                   "refresh_interval_seconds": 20, "quote_size_shares": 20, "max_markets": 30,
                   "max_position_shares": 200, "post_only": True, "pull_quotes_before_events": []},
        "risk": {"max_price_deviation_from_fair_value": 0.03},
    }  # fmt: skip
    assert load_quoter_config(s) == CFG
    s["quoter"]["refresh_interval_seconds"] = 30
    with pytest.raises(ValueError, match="refresh"):
        load_quoter_config(s)


# --- Quoter cycle -------------------------------------------------------------------


class FakeRouter:
    def __init__(self):
        self.calls: list[list] = []
        self.blocked = False

    async def replace_all(self, orders_with_fv, now):
        self.calls.append(list(orders_with_fv))

        class R:
            placed = len(orders_with_fv)
            blocked = ""

        return R()


class FakeTracker:
    def __init__(self, values):
        self.values = values

    def current(self, exchange_id):
        return self.values.get(exchange_id, FairValue.none("no fair value yet"))


def targets(n):
    return [QuoteTarget(exchange_id=f"e{i}", market_id=f"m{i}", race_key=f"R{i}", party="D") for i in range(n)]


def make_quoter(values, books=None, positions=None, cfg=CFG):
    router = FakeRouter()
    control = TradingControl()

    async def book_reader(ids):
        return books or {}

    q = Quoter(router=router, fair_values=FakeTracker(values), positions=lambda: positions or {},
               books=book_reader, cfg=cfg, tournament_id=TID, control=control)  # fmt: skip
    return q, router, control


def run(c):
    return asyncio.run(c)


def test_cycle_builds_two_orders_per_market_with_expiry_and_metadata():
    q, router, _ = make_quoter({"e0": fv(), "e1": fv(0.3)})
    run(q.cycle(targets(2), NOW))
    orders = [o for o, _ in router.calls[0]]
    assert len(orders) == 4
    bid = orders[0]
    assert (bid.side, bid.action, bid.price, bid.quantity) == ("yes", "buy", 0.49, 20)
    assert bid.expiration_date == NOW + timedelta(seconds=30)
    assert (bid.race_key, bid.party_id, bid.market_id, bid.tournament_id) == ("R0", "D", "m0", TID)
    assert orders[1].action == "sell" and orders[1].price == 0.51
    assert len({o.idempotency_key for o in orders}) == 4


def test_markets_without_fair_value_are_left_out_and_pulled():
    q, router, _ = make_quoter({"e0": fv()})
    run(q.cycle(targets(3), NOW))
    assert {o.exchange_id for o, _ in router.calls[0]} == {"e0"}


def test_no_requote_when_nothing_changed_before_refresh_interval():
    q, router, _ = make_quoter({"e0": fv()})
    run(q.cycle(targets(1), NOW))
    run(q.cycle(targets(1), NOW + timedelta(seconds=5)))
    assert len(router.calls) == 1


def test_requote_on_fair_value_move_of_a_point():
    values = {"e0": fv(0.5)}
    q, router, _ = make_quoter(values)
    run(q.cycle(targets(1), NOW))
    values["e0"] = fv(0.505)
    run(q.cycle(targets(1), NOW + timedelta(seconds=1)))
    assert len(router.calls) == 1  # half a point: no
    values["e0"] = fv(0.511)
    run(q.cycle(targets(1), NOW + timedelta(seconds=2)))
    assert len(router.calls) == 2


def test_requote_before_quotes_expire():
    q, router, _ = make_quoter({"e0": fv()})
    run(q.cycle(targets(1), NOW))
    run(q.cycle(targets(1), NOW + timedelta(seconds=20)))
    assert len(router.calls) == 2


def test_losing_a_fair_value_triggers_pull():
    values = {"e0": fv(), "e1": fv()}
    q, router, _ = make_quoter(values)
    run(q.cycle(targets(2), NOW))
    del values["e1"]
    run(q.cycle(targets(2), NOW + timedelta(seconds=1)))
    assert len(router.calls) == 2 and {o.exchange_id for o, _ in router.calls[1]} == {"e0"}


def test_max_markets_cap():
    cfg = QuoterConfig(**{**CFG.__dict__, "max_markets": 25})
    q, router, _ = make_quoter({f"e{i}": fv() for i in range(40)}, cfg=cfg)
    run(q.cycle(targets(40), NOW))
    assert len({o.exchange_id for o, _ in router.calls[0]}) == 25


def test_halted_control_means_no_cycle():
    q, router, control = make_quoter({"e0": fv()})
    control.halt("KILL")
    run(q.cycle(targets(1), NOW))
    assert router.calls == []


def test_blackout_pulls_everything():
    cfg = QuoterConfig(**{**CFG.__dict__, "blackouts": ((NOW, NOW + timedelta(hours=1)),)})
    q, router, _ = make_quoter({"e0": fv()}, cfg=cfg)
    run(q.cycle(targets(1), NOW + timedelta(minutes=1)))
    assert router.calls == [[]]


def test_books_feed_post_only():
    q, router, _ = make_quoter({"e0": fv(0.5)}, books={"e0": (0.40, 0.47)})
    run(q.cycle(targets(1), NOW))
    assert [o.price for o, _ in router.calls[0]] == [0.465, 0.51]


def test_positions_feed_inventory_skew():
    q, router, _ = make_quoter({"e0": fv(0.5)}, positions={"e0": 100})
    run(q.cycle(targets(1), NOW))
    assert [o.price for o, _ in router.calls[0]] == [0.48, 0.5]
