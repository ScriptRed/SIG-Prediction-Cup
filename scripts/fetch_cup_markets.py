"""Fetch every Cup market via documented endpoints and write
data/cup_markets.csv.

    python -m scripts.fetch_cup_markets

Requires SIG_API_KEY in the environment (.env). Uses only documented reads:
GET /tournaments to resolve the Cup's tournamentId, then GET /markets
scoped to it (docs/platform/SUMMARY.md). On any read failure this prints
the exact error and exits non-zero rather than guessing or writing a
partial file — callers should fall back to an existing data/cup_markets.csv
or mock data instead of trading on a broken fetch.
"""

from __future__ import annotations

import csv
import os
import sys

import httpx
from dotenv import load_dotenv

from predcup.cup_markets import build_rows

BASE_URL = "https://www.thesuper.market/api/v1"
# TODO(api): confirm this slug stays "midterm-elections" if SIG ever renames
# the tournament; resolved dynamically below rather than hardcoding the id.
CUP_SLUG = "midterm-elections"
OUTPUT_PATH = "data/cup_markets.csv"
FIELDNAMES = [
    "id", "exchange_id", "title", "category", "state", "office", "district",
    "party", "race_key",
]  # fmt: skip


def fetch_tournament_id(client: httpx.Client, headers: dict[str, str]) -> tuple[str, str]:
    resp = client.get(f"{BASE_URL}/tournaments", headers=headers)
    resp.raise_for_status()
    for t in resp.json()["data"]:
        if t["slug"] == CUP_SLUG:
            return t["id"], t["slug"]
    raise RuntimeError(f"tournament slug {CUP_SLUG!r} not found among accessible tournaments")


def fetch_all_markets(
    client: httpx.Client, headers: dict[str, str], tournament_id: str
) -> list[dict]:
    all_markets: list[dict] = []
    cursor: str | None = None
    while True:
        params = {"tournamentId": tournament_id, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = client.get(f"{BASE_URL}/markets", headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        all_markets.extend(data["data"])
        if not data["pagination"]["hasMore"]:
            break
        cursor = data["pagination"]["nextCursor"]
    return all_markets


def write_csv(rows: list[dict[str, str]], path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    load_dotenv()
    key = os.environ.get("SIG_API_KEY")
    if not key:
        print("SIG_API_KEY not set (check .env)", file=sys.stderr)
        return 1

    headers = {"Authorization": f"Bearer {key}"}
    try:
        with httpx.Client(timeout=15) as client:
            tournament_id, slug = fetch_tournament_id(client, headers)
            raw_markets = fetch_all_markets(client, headers, tournament_id)
    except httpx.HTTPStatusError as e:
        print(
            f"SIG API read failed: {e.response.status_code} {e.response.text}",
            file=sys.stderr,
        )
        return 1
    except httpx.HTTPError as e:
        print(f"SIG API read failed: {e!r}", file=sys.stderr)
        return 1

    rows = build_rows(raw_markets)
    write_csv(rows, OUTPUT_PATH)
    print(f"wrote {len(rows)} markets to {OUTPUT_PATH} (tournament {slug!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
