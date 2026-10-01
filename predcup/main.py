"""Entry point: python -m predcup.main [--duration SECONDS]

Reads config/settings.yaml and .env (SIG_API_KEY), resolves the Cup
tournament, and runs predcup.app.App: in SHADOW mode (while
predcup.app.LIVE_ENABLED is False) fair values and quotes are computed,
risk-checked and logged to events_log (`shadow_quote`), nothing is placed
or cancelled.

Safety: the KILL file watcher and Telegram /kill /resetramp (restricted to
TELEGRAM_CHAT_ID) are wired to App.kill / App.reset_ramp. Telegram is
required in live mode. SIGINT/SIGTERM (systemd stops with SIGINT) end
App.serve(), whose finally block cancels all Cup orders in live mode.
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

from predcup.app import LIVE_ENABLED, App, LogAlerter, load_targets
from predcup.edge_watch import register_edge_watcher
from predcup.killfile import KillFileWatcher
from predcup.market_map import fusion_race_keys
from predcup.seed_lag import register_seed_lag_logger
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


def build_watchdog(settings: dict, alerter, env=None):
    """The systemd watchdog, only when running under systemd (NOTIFY_SOCKET
    set). On a laptop there is nothing to ping and nothing to alert about."""
    env = os.environ if env is None else env
    if not env.get("NOTIFY_SOCKET"):
        return None
    from predcup.watchdog import Watchdog

    return Watchdog.from_config(settings, alerter)


def build_alerter(settings: dict, store: EventStore, shadow: bool):
    """(alerter, bot) - Telegram when configured; required in live mode.
    Returns bot=None when Telegram isn't configured (shadow only)."""
    from predcup.alerts import TelegramAlerter, TelegramBot, load_telegram_config, load_telegram_env

    try:
        token, chat_id = load_telegram_env()
    except RuntimeError as e:
        if not shadow:
            raise RuntimeError(f"live mode needs Telegram for /kill and alerts: {e}") from e
        print(f"WARNING: {e}; alerts go to events_log and stderr only, no Telegram /kill", file=sys.stderr)
        return LogAlerter(store), None, None
    tg = load_telegram_config(settings)
    bot = TelegramBot(token)
    alerter = TelegramAlerter(chat_id, bot.send_message, tg.min_send_interval_seconds, tg.max_send_attempts,
                              event_store=store)  # fmt: skip
    return alerter, bot, chat_id


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
    shadow = not LIVE_ENABLED

    store = EventStore(settings["storage"]["db_path"])
    alerter, bot, chat_id = build_alerter(settings, store, shadow)
    relay = RateLimitRelay(store)
    cup_rows, map_rows = _read_csv(MARKETS_PATH), _read_csv(MAP_PATH)
    targets = load_targets(cup_rows, map_rows, manual_only=settings["trading"]["manual_only"])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
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
                      market_meta={c["exchange_id"]: (c["id"], c["party"], c["race_key"]) for c in cup_rows},
                      fusion_race_keys=fusion_race_keys(cup_rows, map_rows), shadow=shadow,
                      live_allowed=LIVE_ENABLED)  # fmt: skip
            relay.target = app.risk.record_rate_limited

            # Hard rule 7: the KILL file always works, Telegram /kill when configured.
            watcher = KillFileWatcher.from_config(settings, app.kill, alerter=alerter)
            app.add_task(watcher.run)
            watchdog = build_watchdog(settings, alerter)
            if watchdog is not None:
                app.set_watchdog(watchdog)  # READY=1 once the loops run, then pings
            app.add_task(app.daily_summary(alerter).run)
            register_edge_watcher(app, settings, cup_rows, map_rows)  # alerts only; off unless enabled
            register_seed_lag_logger(app, settings, cup_rows, map_rows)  # read-only; off unless enabled
            if bot is not None:
                from predcup.alerts import CommandRouter, load_telegram_config

                tg = load_telegram_config(settings)
                router = CommandRouter(chat_id, on_kill=app.kill, on_reset_ramp=app.reset_ramp,
                                       confirm_timeout_seconds=tg.confirm_timeout_seconds, event_store=store,
                                       status_provider=app.status_provider())  # fmt: skip
                app.add_task(alerter.run)
                await bot.start(router)

            mode = "shadow" if shadow else "LIVE"
            print(f"{mode} mode: {len(targets)} quotable market(s), bankroll {bankroll:,.0f}, tournament {slug}",
                  file=sys.stderr)  # fmt: skip
            if not targets:
                print("no verified Tier A rows in market_map.csv: fair values and quotes will be empty "
                      "(mark races with scripts.show_mapping --mark-verified)", file=sys.stderr)  # fmt: skip
            alerter.send(f"predcup started ({mode}): {len(targets)} quotable market(s)")
            # serve(): startup cancel-all (live), then the loops; its finally
            # cancels all Cup orders in live mode.
            await app.serve(stop, duration)
    finally:
        if bot is not None:
            try:
                await alerter.flush()
            finally:
                await bot.stop()
        store.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration", type=float, default=None, help="stop after this many seconds")
    args = parser.parse_args()
    return asyncio.run(amain(args.duration))


if __name__ == "__main__":
    raise SystemExit(main())
