"""Read-only status check, used after a restart (docs/deploy.md
"Restart after a kill"):

    python -m scripts.bot_status

Prints: whether a KILL file is present, open Cup orders (live read via
GET /orders?status=open, tournament-scoped), and the latest app_start,
reconciliation, kill and shutdown events from the bot's SQLite. Exit code
0 only if there is no KILL file, no open Cup order, and the latest
reconciliation after the latest start is clean. Places nothing.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv

from predcup.store import EventStore
from predcup.venues.sig import SigVenue


def latest(store: EventStore, event_type: str) -> dict[str, Any] | None:
    events = store.all_events(event_type)
    return events[-1] if events else None


def status_lines(store: EventStore, open_order_ids: list[str], kill_file_present: bool) -> tuple[list[str], bool]:
    ok = True
    lines = []
    if kill_file_present:
        ok = False
        lines.append("KILL file present: the bot halts again at start. Delete it to resume.")
    if open_order_ids:
        ok = False
        lines.append(f"{len(open_order_ids)} open Cup order(s): {open_order_ids}")
    else:
        lines.append("No open Cup orders.")
    start, recon = latest(store, "app_start"), latest(store, "reconciliation")
    for name in ("app_start", "kill", "shutdown"):
        e = latest(store, name)
        lines.append(f"latest {name}: " + (f"{e['ts']} {e['payload']}" if e else "none"))
    if recon is None or (start is not None and recon["ts"] < start["ts"]):
        ok = False
        lines.append("No reconciliation since the latest start yet (runs every minute): check again shortly.")
    else:
        lines.append(f"latest reconciliation: {recon['ts']} {recon['payload']}")
        if recon["payload"].get("status") != "clean":
            ok = False
    lines.append("OK" if ok else "NOT OK")
    return lines, ok


async def _open_orders(settings: dict) -> list[str]:
    load_dotenv(".env")
    async with httpx.AsyncClient(timeout=15) as client:
        venue = SigVenue(client, base_url=settings["platform"]["base_url"], api_key=os.environ["SIG_API_KEY"],
                         tournament_slug=settings["platform"]["tournament_slug"],
                         on_rate_limited=lambda endpoint, retry_after: None)  # fmt: skip
        tid = await venue.tournament_id()
        return [o.id or "" for o in await venue.get_open_orders(tid)]


def main() -> int:
    settings = yaml.safe_load(Path("config/settings.yaml").read_text())
    store = EventStore(settings["storage"]["db_path"])
    lines, ok = status_lines(store, asyncio.run(_open_orders(settings)), Path(settings["kill_switch"]["file_path"]).exists())
    print("\n".join(lines))
    store.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
