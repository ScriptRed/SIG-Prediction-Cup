"""Cut cancel volume (2026-10-13 live run: three 429s on POST
/orders/cancel-all, Retry-After 60, ~28 cancel-alls in the first minute).

- The quoter re-quotes a market only when its fair value moves more than
  quoter.requote_fair_value_move (1 point), its position changed, or its
  quotes are within quoter.requote_before_expiry_seconds of the expiry the
  router actually stamped. Pulls go first, then the most urgent.
- The router paces scoped cancel-alls (orders.cancel_all_max_per_window per
  orders.cancel_all_window_seconds); markets over the budget wait for a
  later cycle. A 429 with a long Retry-After is not sat out inline: the
  adapter gives up (SigRateLimited), the router posts what is already
  cancelled with a fresh expiry and pauses every re-quote until Retry-After
  has passed. Quotes priced longer ago than orders.max_quote_age_seconds
  are never posted.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from _helpers import full_size_ramp
from predcup.control import TradingControl
from predcup.fairvalue import FairValue
from predcup.models import Order
from predcup.orders import CancelPacer, OrderRouter, SendRules, load_send_rules
from predcup.risk import RiskLimits, RiskManager
from predcup.store import EventStore
from predcup.strategies.quoter import QuoterConfig, QuoteTarget, Quoter
from predcup.venues.sig import SigRateLimited, SigVenue
from sim.mock_exchange import MockExchange

TID = "550e8400-e29b-41d4-a716-446655440000"
T0 = datetime(2026, 10, 13, 16, 7, 59, tzinfo=timezone.utc)
RULES = SendRules(quote_expiration_seconds=120, min_expiry_margin_seconds=10, cancel_all_max_per_window=3,
                  cancel_all_window_seconds=60, max_quote_age_seconds=10, rate_limit_pause_seconds=60)  # fmt: skip


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += timedelta(seconds=s)


class Alerts:
    def __init__(self):
        self.messages = []

    def send(self, m):
        self.messages.append(m)


# --- config -----------------------------------------------------------------------


def test_send_rules_from_settings():
    s = {"quoter": {"quote_expiration_seconds": 120},
         "orders": {"min_expiry_margin_seconds": 10, "cancel_all_max_per_window": 3, "cancel_all_window_seconds": 60,
                    "max_quote_age_seconds": 10, "rate_limit_pause_seconds": 60}}  # fmt: skip
    assert load_send_rules(s) == RULES
    s["orders"]["cancel_all_max_per_window"] = 0
    with pytest.raises(ValueError):
        load_send_rules(s)


# --- the pacer --------------------------------------------------------------------


def test_pacer_allows_at_most_n_per_sliding_window():
    p = CancelPacer(max_calls=3, window_seconds=60)
    assert [p.take(T0) for _ in range(4)] == [True, True, True, False]
    assert not p.take(T0 + timedelta(seconds=59))
    assert p.take(T0 + timedelta(seconds=60)) and p.take(T0 + timedelta(seconds=60))


def test_pacer_pause_blocks_until_retry_after_has_passed():
    p = CancelPacer(max_calls=10, window_seconds=60)
    p.pause_until(T0 + timedelta(seconds=60))
    p.pause_until(T0 + timedelta(seconds=5))  # a shorter pause never shortens a longer one
    assert not p.take(T0 + timedelta(seconds=59))
    assert p.paused_until(T0 + timedelta(seconds=59)) == T0 + timedelta(seconds=60)
    assert p.take(T0 + timedelta(seconds=60))


# --- the router -------------------------------------------------------------------


class Venue(MockExchange):
    def __init__(self, clock, rate_limited=(), cancel_seconds=0.0):
        super().__init__()
        self.clock = clock
        self.rate_limited = set(rate_limited)  # exchange ids whose cancel-all answers 429
        self.cancel_seconds = cancel_seconds
        self.cancels: list[tuple[datetime, str]] = []
        self.sent: list[tuple[datetime, list[Order]]] = []
        self.batch_429 = False

    async def cancel_all(self, tournament_id, exchange_id=None, market_id=None):
        self.cancels.append((self.clock(), exchange_id))
        self.clock.advance(self.cancel_seconds)
        if exchange_id in self.rate_limited:
            raise SigRateLimited(429, "RATE_LIMITED", "slow down", retry_after=60.0)
        return await super().cancel_all(tournament_id, exchange_id, market_id)

    async def place_batch(self, orders, batch_key):
        self.sent.append((self.clock(), list(orders)))
        if self.batch_429:
            raise SigRateLimited(429, "RATE_LIMITED", "slow down", retry_after=60.0)
        return await super().place_batch(orders, batch_key)


def make_router(tmp_path, venue, clock, rules=RULES):
    store = EventStore(tmp_path / "e.db")
    risk = RiskManager(
        limits=RiskLimits(max_bankroll_fraction_per_market=1.0, max_total_exposure_fraction=1.0,
                          max_party_exposure_fraction=1.0, max_order_size_susqies=1_000_000,
                          max_price_deviation_from_fair_value=0.05, daily_loss_stop_fraction=1.0,
                          stale_data_stop_seconds=600),  # fmt: skip
        bankroll=100_000.0, event_store=store, venue=venue, tournament_id=TID, alerter=Alerts(),
        size_ramp=full_size_ramp(), fusion_race_keys=frozenset(), now=clock,
    )  # fmt: skip
    router = OrderRouter(venue=venue, risk=risk, store=store, tournament_id=TID, shadow=False, alerter=Alerts(),
                         control=TradingControl(), send_rules=rules, clock=clock)  # fmt: skip
    return router, store, risk


def order(ex, key, clock):
    return Order(exchange_id=ex, market_id=f"m{ex}", tournament_id=TID, party_id="D", race_key=f"R{ex}", side="yes",
                 action="buy", quantity=10, price=0.49, idempotency_key=key,
                 expiration_date=clock() + timedelta(seconds=120))  # fmt: skip


def updates(clock, *exs):
    f = FairValue(ok=True, value=0.5, uncertainty=0.01, as_of=clock() - timedelta(seconds=2))
    return {ex: [(order(ex, f"{ex}-{clock().timestamp()}", clock), f)] for ex in exs}


def test_cancels_over_the_budget_wait_for_a_later_cycle(tmp_path):
    clock = Clock()
    venue = Venue(clock)
    router, store, _ = make_router(tmp_path, venue, clock)
    res = run(router.requote(updates(clock, "a", "b", "c", "d", "e"), clock()))
    assert [ex for _, ex in venue.cancels] == ["a", "b", "c"]  # in the quoter's priority order
    assert res.done == {"a", "b", "c"} and res.deferred == {"d", "e"}
    assert {o.exchange_id for _, b in venue.sent for o in b} == {"a", "b", "c"}
    assert store.all_events("requote_deferred")[0]["payload"]["markets"] == ["d", "e"]
    clock.advance(30)
    res = run(router.requote(updates(clock, "d", "e"), clock()))
    assert res.done == frozenset() and len(venue.cancels) == 3  # still inside the window
    clock.advance(31)
    res = run(router.requote(updates(clock, "d", "e"), clock()))
    assert res.done == {"d", "e"}


def test_cancel_429_posts_the_rest_fresh_and_pauses_all_requotes(tmp_path):
    clock = Clock()
    venue = Venue(clock, rate_limited={"c"})
    rules = SendRules(**{**RULES.__dict__, "cancel_all_max_per_window": 50})
    router, store, _ = make_router(tmp_path, venue, clock, rules)
    res = run(router.requote(updates(clock, "a", "b", "c"), clock()))
    assert res.done == {"a", "b"}  # c's old quotes may still rest: not re-posted
    [(sent_at, batch)] = venue.sent
    assert sent_at == T0 and all(o.expiration_date == T0 + timedelta(seconds=120) for o in batch)
    # Retry-After 60: no cancel at all (nor any post) until it has passed
    for wait in (1, 30, 28):
        clock.advance(wait)
        res = run(router.requote(updates(clock, "c"), clock()))
        assert res.done == frozenset() and res.deferred == {"c"}
    assert len(venue.cancels) == 3
    clock.advance(1)  # T0 + 60
    venue.rate_limited.clear()
    res = run(router.requote(updates(clock, "c"), clock()))
    assert res.done == {"c"}
    assert venue.sent[-1][1][0].expiration_date == clock() + timedelta(seconds=120)


def test_whole_batch_429_places_nothing_and_pauses(tmp_path):
    clock = Clock()
    venue = Venue(clock)
    venue.batch_429 = True
    router, store, risk = make_router(tmp_path, venue, clock)
    res = run(router.requote(updates(clock, "a"), clock()))
    assert res.placed == 0 and res.done == {"a"} and res.expires == {}  # swept, nothing resting
    assert risk._tracked_orders() == []
    assert not router.blocked  # a 429 places nothing: no reconciliation needed
    clock.advance(10)
    res = run(router.requote(updates(clock, "a"), clock()))
    assert res.deferred == {"a"} and len(venue.sent) == 1


def test_router_reports_the_expiry_it_stamped(tmp_path):
    clock = Clock()
    venue = Venue(clock, cancel_seconds=2)
    router, _, _ = make_router(tmp_path, venue, clock)
    res = run(router.requote(updates(clock, "a"), clock()))
    assert res.expires == {"a": T0 + timedelta(seconds=2 + 120)}


def test_quotes_priced_too_long_ago_are_not_posted(tmp_path):
    clock = Clock()
    venue = Venue(clock, cancel_seconds=11)  # slow cancels: past max_quote_age_seconds
    router, store, risk = make_router(tmp_path, venue, clock)
    built_at = clock()
    res = run(router.requote(updates(clock, "a"), built_at))
    assert venue.sent == [] and res.done == {"a"} and res.expires == {}
    assert risk._tracked_orders() == []
    assert store.all_events("requote_stale")[0]["payload"]["age_seconds"] == pytest.approx(11)


def test_kill_switch_cancel_is_never_paced(tmp_path):
    clock = Clock()
    venue = Venue(clock)
    router, _, risk = make_router(tmp_path, venue, clock)
    run(router.requote(updates(clock, "a", "b", "c"), clock()))  # budget used up
    result = run(risk.kill_switch())
    assert result.success and venue.cancels[-1][1] is None  # tournament-wide, at once


# --- the quoter ---------------------------------------------------------------------

CFG = QuoterConfig(min_edge=0.01, uncertainty_coefficient=1.0, inventory_skew_coefficient=0.02, requote_move=0.01,
                   quote_size=20, expiration_seconds=120, requote_before_expiry_seconds=30, max_markets=30,
                   max_position_shares=200, max_price_deviation=0.03, post_only=True, blackouts=())  # fmt: skip


class FakeRouter:
    def __init__(self, ttl=120):
        self.calls = []
        self.ttl = ttl
        self.defer: set[str] = set()
        self.unplaced: set[str] = set()

    async def requote(self, updates, now):
        self.calls.append({ex: list(v) for ex, v in updates.items()})
        done = frozenset(ex for ex in updates if ex not in self.defer)
        expires = {ex: now + timedelta(seconds=self.ttl) for ex in done if updates[ex] and ex not in self.unplaced}

        class R:
            pass

        r = R()
        r.blocked, r.done, r.expires, r.deferred = "", done, expires, frozenset(updates) - done
        return r


class Tracker:
    def __init__(self, values):
        self.values = values

    def current(self, ex):
        return self.values.get(ex, FairValue.none("none"))


def fv(v):
    return FairValue(ok=True, value=v, uncertainty=0.01, as_of=T0)


def targets(*exs):
    return [QuoteTarget(exchange_id=ex, market_id=f"m{ex}", race_key=f"R{ex}", party="D") for ex in exs]


def make_quoter(values, positions=None):
    router = FakeRouter()

    async def books(ids):
        return {}

    q = Quoter(router=router, fair_values=Tracker(values), positions=lambda: positions or {}, books=books,
               cfg=CFG, tournament_id=TID, control=TradingControl())  # fmt: skip
    return q, router


def test_no_requote_every_cycle_only_on_a_move_over_one_point_or_near_expiry():
    values = {"a": fv(0.5)}
    q, router = make_quoter(values)
    run(q.cycle(targets("a"), T0))
    for i in range(1, 18):  # 5 s cycles up to 85 s: fair value wobbles by up to a point
        values["a"] = fv(0.5 + (0.01 if i % 2 else -0.01))
        run(q.cycle(targets("a"), T0 + timedelta(seconds=5 * i)))
    assert len(router.calls) == 1
    run(q.cycle(targets("a"), T0 + timedelta(seconds=90)))  # 30 s left on a 120 s expiry
    assert len(router.calls) == 2


def test_a_move_over_one_point_requotes():
    values = {"a": fv(0.5), "b": fv(0.3)}
    q, router = make_quoter(values)
    run(q.cycle(targets("a", "b"), T0))
    values["a"] = fv(0.5101)
    run(q.cycle(targets("a", "b"), T0 + timedelta(seconds=5)))
    assert list(router.calls[1]) == ["a"]


def test_near_expiry_uses_the_expiry_the_router_stamped():
    values = {"a": fv(0.5)}
    q, router = make_quoter(values)
    router.ttl = 60  # e.g. the batch went out late
    run(q.cycle(targets("a"), T0))
    run(q.cycle(targets("a"), T0 + timedelta(seconds=30)))
    assert len(router.calls) == 2


def test_deferred_market_is_tried_again_next_cycle():
    q, router = make_quoter({"a": fv(0.5), "b": fv(0.5)})
    router.defer = {"b"}
    run(q.cycle(targets("a", "b"), T0))
    router.defer = set()
    run(q.cycle(targets("a", "b"), T0 + timedelta(seconds=5)))
    assert list(router.calls[1]) == ["b"]


def test_swept_market_with_nothing_placed_is_quoted_again():
    q, router = make_quoter({"a": fv(0.5)})
    router.unplaced = {"a"}
    run(q.cycle(targets("a"), T0))
    router.unplaced = set()
    run(q.cycle(targets("a"), T0 + timedelta(seconds=5)))
    assert len(router.calls) == 2


def test_pulls_go_first_then_the_most_urgent():
    values = {"a": fv(0.5), "b": fv(0.5), "c": fv(0.5), "d": fv(0.5)}
    positions: dict = {}
    q, router = make_quoter(values, positions)
    run(q.cycle(targets("a", "b", "c", "d"), T0))
    del values["d"]  # lost fair value: pull
    values["b"] = fv(0.53)  # 3 points
    values["c"] = fv(0.515)  # 1.5 points
    positions["a"] = 20  # filled
    run(q.cycle(targets("a", "b", "c", "d"), T0 + timedelta(seconds=5)))
    assert list(router.calls[1]) == ["d", "a", "b", "c"]


# --- the SIG adapter: re-quote calls don't sit out a long Retry-After -------------------


def make_sig(responses, clock):
    sleeps, sent, limited = [], [], []

    async def fake_sleep(s):
        sleeps.append(s)
        clock.advance(s)

    def handler(request):
        if request.url.path.endswith("/tournaments/cup"):
            return httpx.Response(200, json={"id": TID})
        sent.append(request)
        status, body, headers = responses.pop(0)
        return httpx.Response(status, json=body, headers=headers or {})

    venue = SigVenue(httpx.AsyncClient(transport=httpx.MockTransport(handler)), base_url="https://sig.test/api/v1",
                     api_key="k", tournament_slug="cup", on_rate_limited=lambda e, ra: limited.append(ra),
                     sleep=fake_sleep, max_retries=4, clock=clock, min_expiry_margin_seconds=10,
                     max_requote_retry_wait_seconds=5)  # fmt: skip
    return venue, sleeps, sent, limited


RL = {"error": {"code": "RATE_LIMITED", "message": "slow down"}}


def test_scoped_cancel_all_gives_up_on_a_long_retry_after():
    clock = Clock()
    venue, sleeps, sent, limited = make_sig([(429, RL, {"Retry-After": "60"})], clock)
    with pytest.raises(SigRateLimited) as e:
        run(venue.cancel_all(TID, exchange_id="1060"))
    assert e.value.retry_after == 60 and sleeps == [] and len(sent) == 1
    assert limited == [60.0]  # still reaches the size ramp


def test_scoped_cancel_all_still_retries_a_short_retry_after():
    clock = Clock()
    venue, sleeps, sent, _ = make_sig([(429, RL, {"Retry-After": "2"}), (200, {"cancelled": 1, "errors": []}, None)], clock)
    assert run(venue.cancel_all(TID, exchange_id="1060")).cancelled == 1
    assert sleeps == [2.0]


def test_tournament_wide_cancel_all_waits_retry_after_out():
    # The kill switch / shutdown: must finish, so it waits as long as asked.
    clock = Clock()
    venue, sleeps, sent, _ = make_sig([(429, RL, {"Retry-After": "60"}), (200, {"cancelled": 4, "errors": []}, None)], clock)
    assert run(venue.cancel_all(TID)).cancelled == 4
    assert sleeps == [60.0]


def test_whole_batch_429_gives_up_and_says_nothing_was_placed():
    clock = Clock()
    venue, sleeps, sent, _ = make_sig([(429, {"results": [{"index": 0, "ok": False, "status": 429, "data": {}}]},
                                        {"Retry-After": "60"})], clock)  # fmt: skip
    with pytest.raises(SigRateLimited):
        run(venue.place_batch([order("e1", "k", clock)], "b1"))
    assert sleeps == [] and len(sent) == 1


def test_partial_207_with_a_long_retry_after_returns_what_landed():
    clock = Clock()
    partial = {"results": [{"index": 0, "ok": True, "status": 201, "data": {"orderId": 7, "open": True}},
                           {"index": 1, "ok": False, "status": 429, "data": {"code": "RATE_LIMITED"}}]}  # fmt: skip
    venue, sleeps, sent, _ = make_sig([(207, partial, {"Retry-After": "60"})], clock)
    res = run(venue.place_batch([order("e1", "k1", clock), order("e2", "k2", clock)], "b2"))
    assert [(r.ok, r.status) for r in res] == [(True, 201), (False, 429)]
    assert sleeps == [] and len(sent) == 1
