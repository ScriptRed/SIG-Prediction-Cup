"""Pure logic behind scripts/show_mapping.py: reading config/market_map.csv
by race, the polarity statement, the warnings a human verifier must see,
and the narrowly scoped `verified=true, tier=A` rewrite.

No I/O against any venue here - the script fetches, this module judges.
"""

from __future__ import annotations

import csv
import io
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from predcup.venues.kalshi import KalshiEvent, KalshiMarket

KNOWN_POLARITIES = ("same", "inverted")
_NOT_GENERAL_ELECTION = ("primary", "nominee", "nomination", "caucus")
_YEAR_RE = re.compile(r"\b(20\d\d)\b")
CUP_YEAR = "2026"


@dataclass(frozen=True)
class ReviewThresholds:
    max_mid_diff: float  # SIG vs polarity-adjusted Kalshi mid
    max_kalshi_spread: float
    min_kalshi_volume: float  # contracts, lifetime


# --- CSV reading ------------------------------------------------------------


def read_csv(path: str | Path) -> list[dict[str, str]]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def races_in_order(cup_markets: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    """race_key -> that race's rows of data/cup_markets.csv, file order."""
    races: dict[str, list[dict[str, str]]] = {}
    for m in cup_markets:
        races.setdefault(m["race_key"], []).append(m)
    return races


def resolve_race_key(requested: str, race_keys: list[str]) -> str | None:
    if requested in race_keys:
        return requested
    matches = [k for k in race_keys if k.lower() == requested.lower()]
    return matches[0] if len(matches) == 1 else None


def race_summary(map_rows: list[dict[str, str]]) -> tuple[str, str, str, int]:
    """(tier, verified, confidence, rows with a Kalshi ticker) for --list.
    Mixed values across a race's rows are shown as such, never collapsed."""
    tiers = sorted({r.get("tier", "") or "-" for r in map_rows})
    verified = sorted({r.get("verified", "") for r in map_rows})
    confs = sorted({float(r["confidence"] or 0) for r in map_rows})
    tier = "/".join(tiers) if tiers else "-"
    ver = verified[0] if len(verified) == 1 else "mixed"
    if not confs:
        conf = "-"
    elif len(confs) == 1:
        conf = f"{confs[0]:.2f}"
    else:
        conf = f"{confs[0]:.2f}-{confs[-1]:.2f}"
    with_kalshi = sum(1 for r in map_rows if r.get("kalshi_ticker"))
    return tier, ver or "-", conf, with_kalshi


# --- Polarity and warnings ------------------------------------------------


def polarity_statement(sig_title: str, polarity: str, kalshi: KalshiMarket | None, ticker: str) -> str:
    if not ticker:
        return f"SIG '{sig_title}': no Kalshi ticker mapped"
    outcome = f" ('{kalshi.yes_sub_title}')" if kalshi and kalshi.yes_sub_title else ""
    if polarity == "same":
        return f"SIG '{sig_title}' YES = Kalshi YES on {ticker}{outcome}"
    if polarity == "inverted":
        return f"SIG '{sig_title}' YES = Kalshi NO on {ticker}{outcome}"
    return f"SIG '{sig_title}' vs Kalshi {ticker}: polarity {polarity!r} is not one of {KNOWN_POLARITIES}"


def kalshi_mid_in_sig_terms(kalshi: KalshiMarket, polarity: str) -> float | None:
    mid = kalshi.mid
    if mid is None:
        return None
    if polarity == "same":
        return mid
    if polarity == "inverted":
        return 1 - mid
    return None


def review_warnings(
    *,
    sig_bid: float | None,
    sig_ask: float | None,
    polarity: str,
    ticker: str,
    kalshi: KalshiMarket | None,
    event: KalshiEvent | None,
    th: ReviewThresholds,
) -> list[str]:
    """Everything a verifier must look at before trusting this row."""
    if not ticker:
        return ["no Kalshi ticker mapped - nothing to verify against"]
    if kalshi is None:
        return [f"Kalshi market {ticker} could not be fetched (not found?)"]

    warnings: list[str] = []
    if polarity not in KNOWN_POLARITIES:
        warnings.append(f"polarity {polarity!r} is not 'same' or 'inverted'")

    # Primary / nominee contract instead of the general election.
    names = [kalshi.ticker, kalshi.event_ticker, kalshi.title, kalshi.subtitle, kalshi.yes_sub_title]
    if event:
        names += [event.series_ticker, event.title, event.sub_title]
    name_text = " ".join(names).lower()
    hits = [kw for kw in _NOT_GENERAL_ELECTION if kw in name_text]
    if hits:
        warnings.append(f"looks like a PRIMARY/nominee contract (mentions {', '.join(hits)})")
    elif any(kw in kalshi.rules_primary.lower() for kw in _NOT_GENERAL_ELECTION):
        warnings.append("rules text mentions primary/nominee - check this is the general election")

    # Not the 2026 contest. Only titles/tickers are scanned for years: rules
    # text legitimately mentions e.g. a January 2027 swearing-in.
    other_years = sorted({y for y in _YEAR_RE.findall(" ".join(names)) if y != CUP_YEAR})
    if other_years:
        warnings.append(f"title/ticker mentions year(s) {', '.join(other_years)}, not {CUP_YEAR}")
    # January 2027 is allowed: certification can push expected expiry past
    # year end.
    expiry = kalshi.expected_expiration_time or kalshi.close_time
    if expiry and not expiry.startswith(CUP_YEAR) and not expiry.startswith("2027-01"):
        warnings.append(f"expected expiration {expiry} is outside the {CUP_YEAR} cycle")

    # Price disagreement after applying polarity.
    k_mid = kalshi_mid_in_sig_terms(kalshi, polarity)
    s_mid = (sig_bid + sig_ask) / 2 if sig_bid is not None and sig_ask is not None else None
    if k_mid is not None and s_mid is not None:
        diff = abs(s_mid - k_mid)
        if diff > th.max_mid_diff:
            warnings.append(
                f"SIG mid {s_mid:.3f} vs Kalshi mid {k_mid:.3f} (after polarity) differ by "
                f"{diff * 100:.1f} points > {th.max_mid_diff * 100:.0f} - polarity or mapping may be wrong"
            )
    elif s_mid is None:
        warnings.append("SIG book not two-sided - cannot compare mids")

    if kalshi.spread is None:
        warnings.append("Kalshi book not two-sided (no bid or no ask)")
    elif kalshi.spread > th.max_kalshi_spread:
        warnings.append(
            f"Kalshi spread {kalshi.spread * 100:.1f} points > {th.max_kalshi_spread * 100:.0f}"
        )

    if kalshi.volume < th.min_kalshi_volume:
        warnings.append(f"Kalshi volume very low ({kalshi.volume:,.0f} < {th.min_kalshi_volume:,.0f} contracts)")

    return warnings


# --- Marking verified -------------------------------------------------------


def mark_verified(path: str | Path, platform_ids: set[str]) -> int:
    """Set verified=true and tier=A on exactly the rows whose platform_id is
    in `platform_ids`. Every other line is written back byte-for-byte.
    Returns the number of rows changed; raises if any id is missing."""
    path = Path(path)
    raw = path.read_bytes().decode()
    lines = raw.splitlines(keepends=True)
    header = next(csv.reader([lines[0]]))
    for col in ("platform_id", "verified", "tier"):
        if col not in header:
            raise ValueError(f"{path} has no {col!r} column")
    idx = {name: i for i, name in enumerate(header)}

    found: set[str] = set()
    out = [lines[0]]
    for line in lines[1:]:
        fields = next(csv.reader([line]), [])
        if len(fields) != len(header):
            # A multi-line quoted field would split across lines here; refuse
            # rather than risk rewriting the wrong row.
            raise ValueError(f"unexpected row shape in {path}: {line!r}")
        pid = fields[idx["platform_id"]]
        if pid not in platform_ids:
            out.append(line)
            continue
        if pid in found:
            raise ValueError(f"duplicate platform_id {pid} in {path}")
        found.add(pid)
        fields[idx["verified"]] = "true"
        fields[idx["tier"]] = "A"
        ending = line[len(line.rstrip("\r\n")) :]
        buf = io.StringIO()
        csv.writer(buf, lineterminator=ending).writerow(fields)
        out.append(buf.getvalue())

    missing = platform_ids - found
    if missing:
        raise ValueError(f"platform_id(s) not in {path}: {sorted(missing)}")

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", newline="") as f:
        f.write("".join(out))
    os.replace(tmp, path)
    return len(found)
