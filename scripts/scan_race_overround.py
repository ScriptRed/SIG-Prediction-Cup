"""Sum each race's YES prices from the Cup's live order books and flag
anything outside [99, 101] points. Read-only, shadow mode: prints
findings, places no orders. Run this at launch once quoting starts
(docs/PLAN.md Stage 1 step 8, docs/LAUNCH_CHECKLIST.md).

    python -m scripts.scan_race_overround [--markets data/cup_markets.csv]

Requires SIG_API_KEY in the environment (.env). Uses only documented reads:
GET /tournaments to resolve the Cup's tournamentId, then
GET /exchanges/{id}/price per market (docs/platform/SUMMARY.md). On any
read failure this prints the exact error and exits non-zero.
"""

from __future__ import annotations

import argparse
import collections
import csv
import os
import random
import sys
import time

import httpx
from dotenv import load_dotenv

from predcup.overround import RaceSummary, representative_price, summarize_race

BASE_URL = "https://www.thesuper.market/api/v1"
CUP_SLUG = "midterm-elections"  # TODO(api): confirm/update if SIG renames it
# One request per market, back-to-back, hits 429 fast (docs/platform's rate
# limits are unpublished - see docs/platform/SUMMARY.md). Pace requests and
# retry 429 with backoff + jitter, per CLAUDE.md's retry rules.
REQUEST_DELAY_SECONDS = 0.3
MAX_RETRIES = 5


def _get_with_retry(client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    for attempt in range(MAX_RETRIES):
        resp = client.get(url, **kwargs)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp
        backoff = (2**attempt) + random.uniform(0, 1)
        time.sleep(backoff)
    resp.raise_for_status()
    return resp


def load_races(csv_path: str) -> dict[str, list[dict[str, str]]]:
    races: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            races[row["race_key"]].append(row)
    return races


def fetch_tournament_id(client: httpx.Client, headers: dict[str, str]) -> str:
    resp = _get_with_retry(client, f"{BASE_URL}/tournaments", headers=headers)
    for t in resp.json()["data"]:
        if t["slug"] == CUP_SLUG:
            return t["id"]
    raise RuntimeError(f"tournament slug {CUP_SLUG!r} not found among accessible tournaments")


def fetch_price(
    client: httpx.Client, headers: dict[str, str], exchange_id: str, tournament_id: str
) -> float | None:
    resp = _get_with_retry(
        client,
        f"{BASE_URL}/exchanges/{exchange_id}/price",
        headers=headers,
        params={"tournamentId": tournament_id},
    )
    data = resp.json()
    return representative_price(data.get("latestPrice"), data.get("bestBid"), data.get("bestAsk"))


def scan(
    races: dict[str, list[dict[str, str]]],
    client: httpx.Client,
    headers: dict[str, str],
    tournament_id: str,
) -> list[RaceSummary]:
    results = []
    for race_key, markets in sorted(races.items()):
        party_prices = {}
        for m in markets:
            party_prices[m["party"]] = fetch_price(
                client, headers, m["exchange_id"], tournament_id
            )
            time.sleep(REQUEST_DELAY_SECONDS)
        results.append(summarize_race(race_key, party_prices))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markets", default="data/cup_markets.csv")
    args = parser.parse_args()

    load_dotenv()
    key = os.environ.get("SIG_API_KEY")
    if not key:
        print("SIG_API_KEY not set (check .env)", file=sys.stderr)
        return 1
    headers = {"Authorization": f"Bearer {key}"}

    races = load_races(args.markets)

    try:
        with httpx.Client(timeout=15) as client:
            tournament_id = fetch_tournament_id(client, headers)
            results = scan(races, client, headers, tournament_id)
    except httpx.HTTPStatusError as e:
        print(f"SIG API read failed: {e.response.status_code} {e.response.text}", file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"SIG API read failed: {e!r}", file=sys.stderr)
        return 1

    flagged = [r for r in results if r.status == "flagged"]
    insufficient = [r for r in results if r.status == "insufficient_data"]
    print(
        f"Scanned {len(results)} races: {len(flagged)} flagged, "
        f"{len(insufficient)} with insufficient book data"
    )
    for r in flagged:
        print(f"  FLAG {r.race_key}: {r.sum_points} points  {r.prices}")
    for r in insufficient:
        print(f"  DATA {r.race_key}: missing prices for {r.missing_parties}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
