"""Go-live gate (c) and (d): one tiny live test order, through the order
router and RiskManager.check_test_order like any other order.

    python -m scripts.place_test_order --market 379                 # (c)
    python -m scripts.place_test_order --market 379 --leave-resting # (d), then /kill

--market is required: a SIG market id from data/cup_markets.csv, not in
settings trading.manual_only. Refuses unless the Cup is `active` and past
its startDate (read live from GET /tournaments/{slug}).

Places 1 share far from the market (a YES buy at 0.005, or a YES sell at
0.995 if the book's ask is within 2 points of 0.005), with a short
expirationDate. Confirms it in GET /orders?status=open, cancels it by id,
and confirms it's gone. With --leave-resting it stops after confirming,
so /kill can be tested against it; its expiry (default 10 min) still
removes it if nothing else does.

Runs its own RiskManager on data/test_order.db, so the bot's size ramp is
not touched. Rate limits are respected by the adapter's retry rules.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

from predcup.control import TradingControl
from predcup.models import Order
from predcup.orders import OrderRouter
from predcup.risk import RiskManager, SizeRamp, load_risk_limits, load_size_ramp_config
from predcup.store import EventStore
from predcup.venues.sig import SigVenue, new_idempotency_key

BUY_PRICE = 0.005
SELL_PRICE = 0.995
MIN_CLEARANCE = 0.02  # our price must be at least this far from the opposite best quote


class RefusedError(Exception):
    pass


class _PrintAlerter:
    def send(self, message: str) -> None:
        print(f"ALERT: {message}", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--market", required=True, help="SIG market id (data/cup_markets.csv `id`), e.g. 379")
    p.add_argument("--leave-resting", action="store_true", help="don't cancel: leave it for a /kill test")
    p.add_argument("--expiry-seconds", type=int, default=None, help="default 120, or 600 with --leave-resting")
    return p.parse_args(argv)


def select_market(market: str, cup_rows: list[dict[str, str]], manual_only: list[str]) -> dict[str, str]:
    row = next((c for c in cup_rows if c["id"] == market), None)
    if row is None:
        raise RefusedError(f"{market!r} is not a Cup market id (see data/cup_markets.csv)")
    manual = {str(m) for m in manual_only}
    if row["id"] in manual or row["race_key"] in manual:
        raise RefusedError(f"market {market} ({row['race_key']}) is in trading.manual_only")
    return row


def check_trading_open(summary: dict, now: datetime) -> None:
    status = summary.get("status")
    if status != "active":
        raise RefusedError(f"the Cup tournament is {status!r}, not active: trading hasn't opened")
    start = summary.get("startDate")
    if not start or now < datetime.fromisoformat(start.replace("Z", "+00:00")):
        raise RefusedError(f"trading opens at {start}; it's {now.isoformat()}")


def choose_test_quote(best_bid: float | None, best_ask: float | None) -> tuple[str, float]:
    if best_ask is None or best_ask - BUY_PRICE >= MIN_CLEARANCE - 1e-9:
        return "buy", BUY_PRICE
    if best_bid is None or SELL_PRICE - best_bid >= MIN_CLEARANCE - 1e-9:
        return "sell", SELL_PRICE
    raise RefusedError(f"no price far from the market (bid {best_bid}, ask {best_ask})")


async def run_test_order(
    *,
    router: OrderRouter,
    venue,
    tournament_id: str,
    market: dict[str, str],
    leave_resting: bool,
    expiry_seconds: int,
    now: datetime,
) -> list[str]:
    ex = market["exchange_id"]
    tops = await venue.get_top_of_books([ex], tournament_id)
    best_bid, best_ask = tops.get(ex, (None, None))
    action, price = choose_test_quote(best_bid, best_ask)
    order = Order(exchange_id=ex, market_id=market["id"], tournament_id=tournament_id, party_id=market["party"],
                  race_key=market["race_key"], side="yes", action=action, quantity=1, price=price,
                  expiration_date=now + timedelta(seconds=expiry_seconds), idempotency_key=new_idempotency_key())  # fmt: skip
    lines = [f"{market['race_key']} {market['party']} (market {market['id']}, exchange {ex}): "
             f"book {best_bid}/{best_ask}; test order {action} 1 YES @ {price}, expires in {expiry_seconds}s"]  # fmt: skip

    placed = await router.place_test_order(order)
    if placed is None or not placed.id:
        raise RefusedError("test order refused by risk or the venue (see events_log test_order_check / test_order_failed)")
    lines.append(f"placed: order id {placed.id}")

    open_ids = {o.id for o in await venue.get_open_orders(tournament_id, exchange_id=ex)}
    if placed.id not in open_ids:
        raise RefusedError(f"order {placed.id} not seen in open orders (filled? check the venue by hand)")
    lines.append(f"confirmed: order {placed.id} seen in open orders")

    if leave_resting:
        lines.append(f"left resting for the /kill test: order {placed.id}. After /kill, run "
                     "`python -m scripts.bot_status` and expect 'No open Cup orders.'")  # fmt: skip
        return lines

    await venue.cancel(placed.id, tournament_id)
    open_ids = {o.id for o in await venue.get_open_orders(tournament_id, exchange_id=ex)}
    if placed.id in open_ids:
        raise RefusedError(f"order {placed.id} still open after cancel: cancel it by hand or touch KILL")
    lines.append(f"cancelled: order {placed.id} gone from open orders")
    return lines


async def amain(args: argparse.Namespace) -> int:
    settings = yaml.safe_load(Path("config/settings.yaml").read_text())
    load_dotenv(".env")
    with open("data/cup_markets.csv", newline="") as f:
        cup_rows = list(csv.DictReader(f))
    market = select_market(args.market, cup_rows, settings["trading"]["manual_only"])
    expiry = args.expiry_seconds or (600 if args.leave_resting else 120)

    store = EventStore("data/test_order.db")  # own db: the bot's size ramp is not touched
    alerter = _PrintAlerter()
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            venue = SigVenue(http, base_url=settings["platform"]["base_url"], api_key=os.environ["SIG_API_KEY"],
                             tournament_slug=settings["platform"]["tournament_slug"],
                             on_rate_limited=lambda e, r: store.log("rate_limited", {"endpoint": e, "retry_after": r}))  # fmt: skip
            summary = await venue.tournament_summary()
            now = datetime.now(timezone.utc)
            check_trading_open(summary, now)
            tid = summary["id"]
            risk = RiskManager(limits=load_risk_limits(settings), bankroll=await venue.get_balance(tid),
                               event_store=store, venue=venue, tournament_id=tid, alerter=alerter,
                               size_ramp=SizeRamp(load_size_ramp_config(settings), store, alerter),
                               fusion_race_keys=frozenset())  # fmt: skip
            router = OrderRouter(venue=venue, risk=risk, store=store, tournament_id=tid, shadow=False,
                                 alerter=alerter, control=TradingControl())  # fmt: skip
            for line in await run_test_order(router=router, venue=venue, tournament_id=tid, market=market,
                                             leave_resting=args.leave_resting, expiry_seconds=expiry, now=now):  # fmt: skip
                print(line)
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(amain(args))
    except RefusedError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
