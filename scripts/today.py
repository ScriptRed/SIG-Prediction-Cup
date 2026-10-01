"""What happened today: a terminal summary of events_log.

    python -m scripts.today                    # since local midnight (daily_summary.timezone)
    python -m scripts.today --hours 6
    python -m scripts.today --since 2026-10-01T08:00 --until 2026-10-01T12:00

Fills; markouts at 1/5/30 min averaged per market; risk rejections by
reason; halts (kill, market halts, reconciliation mismatches and read
failures, router blocks, rejected batches, failed cancels); size-ramp
changes; rate-limit events. Times in UTC unless an offset is given; shown
in the daily_summary timezone. Opens the SQLite file read-only, so it is
safe to run while the bot is up.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

SETTINGS_PATH = Path("config/settings.yaml")
MARKETS_PATH = Path("data/cup_markets.csv")
HORIZONS = (1, 5, 30)
RAMP_CHANGES = ("init", "step_up", "step_down", "reset")


def _aware(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def read_events(db: str | Path, since: datetime, until: datetime) -> list[tuple[datetime, str, dict]]:
    conn = sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT ts, event_type, payload FROM events_log WHERE ts >= ? AND ts <= ? ORDER BY ts, id",
            (since.astimezone(timezone.utc).isoformat(), until.astimezone(timezone.utc).isoformat()),
        ).fetchall()
        try:
            actions = dict(conn.execute("SELECT fill_id, action FROM fills").fetchall())
        except sqlite3.OperationalError:
            actions = {}
    finally:
        conn.close()
    out = []
    for ts, et, payload in rows:
        p = json.loads(payload)
        if et == "fill" and p.get("fill_id") in actions:
            p.setdefault("action", actions[p["fill_id"]])
        out.append((datetime.fromisoformat(ts), et, p))
    return out


def market_labels(path: Path) -> dict[str, str]:
    """exchange id -> "race party" from data/cup_markets.csv, if present."""
    if not path.exists():
        return {}
    with open(path, newline="") as f:
        return {r["exchange_id"]: f"{r['race_key']} {r['party']}" for r in csv.DictReader(f)}


def _halt_line(et: str, p: dict) -> str | None:
    if et == "kill":
        return f"kill: {p.get('reason', '')}"
    if et == "market_halted":
        return f"market_halted {p.get('market_id')}: {p.get('status_code')} {p.get('error_code')} {p.get('message', '')}"
    if et == "reconciliation" and p.get("status") in ("mismatch", "read_failed"):
        return f"reconciliation {p['status'].replace('_', ' ')}: {p.get('detail', '')}"
    if et == "router_blocked":
        return f"router_blocked: {p.get('reason', '')}"
    if et == "router_unblocked":
        return f"router_unblocked: {p.get('reason', '')}"
    if et == "batch_rejected":
        return f"batch_rejected: {p.get('status')} {p.get('code')} {p.get('message', '')}"
    if et == "kill_switch" and p.get("result") == "failed":
        return f"kill_switch FAILED: still open {p.get('remaining_order_ids')}"
    if et == "cancel_incomplete":
        return f"cancel_incomplete {p.get('exchange_ids')}: still open {p.get('remaining_order_ids')}"
    return None


def summarize(events: list[tuple[datetime, str, dict]], labels: dict[str, str], tz: ZoneInfo) -> list[str]:
    def label(ex: str) -> str:
        return labels.get(ex, "?")

    def hhmm(ts: datetime) -> str:
        return ts.astimezone(tz).strftime("%m-%d %H:%M")

    out: list[str] = []

    # Fills
    fills = [p for _, et, p in events if et == "fill"]
    if not fills:
        out.append("Fills: 0")
    else:
        shares = sum(int(p.get("quantity", 0)) for p in fills)
        out.append(f"Fills: {len(fills)} ({shares} shares, {len({p.get('exchange_id') for p in fills})} markets)")
        by_ex: dict[str, list[dict]] = defaultdict(list)
        for p in fills:
            by_ex[str(p.get("exchange_id"))].append(p)
        for ex, ps in sorted(by_ex.items(), key=lambda kv: label(kv[0])):
            parts = []
            for side in ("yes", "no"):
                sp = [p for p in ps if p.get("side") == side]
                if sp:
                    q = sum(int(p["quantity"]) for p in sp)
                    avg = sum(float(p["price"]) * int(p["quantity"]) for p in sp) / q
                    acts = Counter(p["action"] for p in sp if p.get("action"))  # from the fills table
                    act = f" ({', '.join(f'{a} {n}' for a, n in sorted(acts.items()))})" if acts else ""
                    parts.append(f"{side.upper()} {q} @ avg {avg:.3f}{act}")
            out.append(f"  {label(ex):<16} ex {ex:<6} {len(ps)} fill(s): " + "; ".join(parts))

    # Markouts per market and horizon
    out.append("")
    marks: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for _, et, p in events:
        if et == "markout":
            marks[str(p["exchange_id"])][int(p["minutes"])].append(float(p["markout"]))
    unavailable = sum(1 for _, et, _p in events if et == "markout_unavailable")
    if not marks:
        out.append(f"Markouts: none (unavailable: {unavailable})")
    else:
        out.append(f"Markouts, mean per share (n), unavailable: {unavailable}")
        out.append(f"  {'ex id':<6} {'market':<16} " + " ".join(f"{f'{h} min':>14}" for h in HORIZONS))
        for ex, by_h in sorted(marks.items(), key=lambda kv: label(kv[0])):
            cells = []
            for h in HORIZONS:
                v = by_h.get(h)
                cells.append(f"{f'{sum(v) / len(v):+.3f} ({len(v)})' if v else '-':>14}")
            out.append(f"  {ex:<6} {label(ex):<16} " + " ".join(cells))

    # Risk rejections
    out.append("")
    reasons = Counter(str(p.get("reason", "?")) for _, et, p in events if et == "risk_rejection")
    out.append(f"Risk rejections: {sum(reasons.values())}")
    for reason, n in reasons.most_common():
        out.append(f"  {n:>4}  {reason}")

    # Halts
    out.append("")
    halts = [(ts, line) for ts, et, p in events if (line := _halt_line(et, p)) is not None]
    out.append(f"Halts: {len(halts)}" if halts else "Halts: none")
    for ts, line in halts:
        out.append(f"  {hhmm(ts)}  {line}")

    # Size ramp
    out.append("")
    ramp = [(ts, p) for ts, et, p in events if et == "size_ramp" and p.get("action") in RAMP_CHANGES]
    out.append(f"Ramp changes: {len(ramp)}")
    for ts, p in ramp:
        line = f"  {hhmm(ts)}  {p['action']:<9} step {p.get('step')}/{p.get('max_step')} ({float(p.get('multiplier', 0)):.0%})"
        if "previous_step" in p:
            line += f" from {p['previous_step']}"
        why = " ".join(str(p[k]) for k in ("failure_kind", "detail") if p.get(k))
        if why:
            line += f": {why}"
        out.append(line)

    # Rate limits
    out.append("")
    rl = Counter()
    max_in_window = 0
    for _, et, p in events:
        if et == "size_ramp" and p.get("action") == "rate_limited":
            rl[str(p.get("detail", "?"))] += 1
            max_in_window = max(max_in_window, int(p.get("rate_limits_in_window", 0)))
        elif et == "rate_limited_before_risk":
            rl[f"{p.get('endpoint', '?')} (before risk)"] += 1
    out.append(f"Rate-limit events: {sum(rl.values())}" + (f", max {max_in_window} in window" if max_in_window else ""))
    for what, n in rl.most_common():
        out.append(f"  {n:>4}  {what}")
    return out


def main(
    argv: list[str] | None = None,
    *,
    out: Callable[[str], None] = print,
    settings_path: Path = SETTINGS_PATH,
    markets_path: Path = MARKETS_PATH,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    settings = yaml.safe_load(settings_path.read_text())
    tz = ZoneInfo(settings["daily_summary"]["timezone"])
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=settings["storage"]["db_path"])
    p.add_argument("--hours", type=float, default=None, help="window ending now")
    p.add_argument("--since", type=_aware, default=None, help="ISO time, UTC if no offset")
    p.add_argument("--until", type=_aware, default=None)
    a = p.parse_args(argv)

    until = a.until or now()
    if a.since is not None:
        since = a.since
    elif a.hours is not None:
        since = until - timedelta(hours=a.hours)
    else:
        since = until.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    if not Path(a.db).exists():
        out(f"no database at {a.db}")
        return 1
    events = read_events(a.db, since, until)
    out(f"{a.db}: {since.astimezone(tz):%Y-%m-%d %H:%M} -> {until.astimezone(tz):%Y-%m-%d %H:%M} {tz.key}, "
        f"{len(events)} events")  # fmt: skip
    out("")
    for line in summarize(events, market_labels(markets_path), tz):
        out(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
