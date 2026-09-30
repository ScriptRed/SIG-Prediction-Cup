"""Entry point: python -m predcup.main [--duration SECONDS]

Reads config/settings.yaml and .env (SIG_API_KEY), resolves the Cup
tournament, and runs predcup.app.App in SHADOW mode: fair values and
quotes are computed, risk-checked and logged to events_log
(`shadow_quote`), nothing is placed or cancelled. Live mode is disabled in
this build (predcup.app.LIVE_ENABLED).

SIGINT/SIGTERM halt through the same path as the KILL switch.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import signal
import sys
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

from predcup.app import App, LogAlerter, load_targets
from predcup.market_map import fusion_race_keys
from predcup.store import EventStore
from predcup.venues.kalshi import KalshiReadOnly
from predcup.venues.sig import SigVenue

SETTINGS_PATH = Path("config/settings.yaml")
MAP_PATH = Path("config/market_map.csv")
MARKETS_PATH = Path("data/cup_markets.csv")


class RateLimitRelay:
    """SigVenue needs its 429 callback before RiskManager exists (App builds
    it). Until `target` is set, 429s are logged, never dropped."""

    def __init__(self, store: EventStore) -> None:
        self._store = store
        self.target = None

    def __call__(self, endpoint: str, retry_after: float | None) -> None:
        if self.target is not None:
            self.target(endpoint, retry_after)
        else:
            self._store.log("rate_limited_before_risk", {"endpoint": endpoint, "retry_after": retry_after})


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


async def amain(duration: float | None) -> int:
    settings = yaml.safe_load(SETTINGS_PATH.read_text())
    load_dotenv(".env")
    api_key = os.environ.get("SIG_API_KEY")
    if not api_key:
        print("SIG_API_KEY not set (check .env)", file=sys.stderr)
        return 1
    slug = settings["platform"].get("tournament_slug")
    if not slug:
        print("platform.tournament_slug not set in settings.yaml", file=sys.stderr)
        return 1

    store = EventStore(settings["storage"]["db_path"])
    alerter = LogAlerter(store)  # Telegram replaces this when the safety branch merges
    relay = RateLimitRelay(store)
    cup_rows, map_rows = _read_csv(MARKETS_PATH), _read_csv(MAP_PATH)
    targets = load_targets(cup_rows, map_rows)

    async with httpx.AsyncClient(timeout=15) as sig_http, httpx.AsyncClient(timeout=15) as kalshi_http:
        venue = SigVenue(sig_http, base_url=settings["platform"]["base_url"], api_key=api_key,
                         tournament_slug=slug, on_rate_limited=relay)  # fmt: skip
        tid = await venue.tournament_id()
        configured = settings["platform"].get("tournament_id")
        if configured and configured != tid:
            print(f"tournament id mismatch: settings {configured}, API {tid}", file=sys.stderr)
            return 1
        bankroll = await venue.get_balance(tid)
        kcfg = settings["venues"]["kalshi"]
        kalshi = KalshiReadOnly(kalshi_http, kcfg["base_url"], kcfg["request_delay_seconds"])

        app = App(settings=settings, venue=venue, kalshi=kalshi, store=store, alerter=alerter,
                  tournament_id=tid, bankroll=bankroll, targets=targets,
                  fusion_race_keys=fusion_race_keys(cup_rows, map_rows), shadow=True)  # fmt: skip
        relay.target = app.risk.record_rate_limited

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, app.request_kill, f"signal {sig.name}")

        print(f"shadow mode: {len(targets)} quotable market(s), bankroll {bankroll:,.0f}, tournament {slug}",
              file=sys.stderr)  # fmt: skip
        if not targets:
            print("no verified Tier A rows in market_map.csv: fair values and quotes will be empty "
                  "(mark races with scripts.show_mapping --mark-verified)", file=sys.stderr)  # fmt: skip

        run = asyncio.create_task(app.run(duration))
        halted = asyncio.create_task(app.control.wait_for_halt())
        done, _ = await asyncio.wait({run, halted}, return_when=asyncio.FIRST_COMPLETED)
        if halted in done:
            await app.handle_halt_once()
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        else:
            halted.cancel()
    store.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration", type=float, default=None, help="stop after this many seconds")
    args = parser.parse_args()
    return asyncio.run(amain(args.duration))


if __name__ == "__main__":
    raise SystemExit(main())
