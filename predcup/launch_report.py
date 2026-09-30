"""Pure logic behind scripts/launch_report.py: one row per mapped SIG
market comparing the Cup book with its Kalshi anchor, the flags that keep
a race off the launch list, and the summary. No venue I/O here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from predcup.mapping_review import kalshi_mid_in_sig_terms, not_2026_reasons
from predcup.market_map import state_in_title
from predcup.venues.kalshi import KalshiEvent, KalshiMarket

# Optional state-party suffix, e.g. Minnesota's "Democratic (DFL) party".
_RULES_PARTY_RE = re.compile(r"\b(Democratic|Republican)(?: \([A-Z-]+\))? [Pp]arty\b")


@dataclass(frozen=True)
class LaunchReportConfig:
    max_gap: float  # SIG mid vs polarity-adjusted Kalshi mid
    longshot_below: float  # Kalshi price under which longshot overpricing is shown
    min_kalshi_volume: float
    exclude_states: frozenset[str]
    exclude_independent: bool


@dataclass(frozen=True)
class SigTop:
    """Top of the Cup book for one exchange (YES-normalized)."""

    bid: float | None
    bid_size: float | None
    ask: float | None
    ask_size: float | None


def sig_top_from_orderbook(book: dict) -> SigTop:
    """GET /exchanges/{id}/orderbook response -> SigTop. Bids are sorted
    descending and asks ascending (docs/platform/openapi.json)."""
    bids, asks = book.get("bids") or [], book.get("asks") or []
    return SigTop(
        bid=book.get("bestBid"),
        bid_size=bids[0]["quantity"] if bids else None,
        ask=book.get("bestAsk"),
        ask_size=asks[0]["quantity"] if asks else None,
    )


def kalshi_party(k: KalshiMarket) -> str | None:
    """Party the Kalshi market resolves on: rules text first ("a
    representative of the Democratic party", "the Republican Party has won
    control"), then independent wording, then the YES label."""
    m = _RULES_PARTY_RE.search(k.rules_primary)
    if m:
        return "D" if m.group(1) == "Democratic" else "R"
    text = f"{k.rules_primary} {k.title} {k.subtitle}".lower()
    if "independent" in text:
        return "I"
    label = k.yes_sub_title.lower()
    if "democrat" in label:
        return "D"
    if "republican" in label:
        return "R"
    return None


def _book_state(bid: float | None, ask: float | None) -> str:
    if bid is None and ask is None:
        return "empty"
    if bid is None or ask is None:
        return "one-sided"
    return "two-sided"


@dataclass
class ReportRow:
    race_key: str
    state: str
    office: str
    party: str
    sig_market_id: str
    sig_exchange_id: str
    sig_title: str
    sig_bid: float | None
    sig_bid_size: float | None
    sig_ask: float | None
    sig_ask_size: float | None
    sig_mid: float | None
    sig_spread: float | None
    kalshi_ticker: str
    polarity: str
    kalshi_bid: float | None
    kalshi_ask: float | None
    kalshi_mid_adj: float | None  # Kalshi mid in SIG YES terms
    kalshi_label: str  # YES label, usually the candidate
    kalshi_party: str | None
    kalshi_event_title: str
    kalshi_volume: float | None
    gap: float | None  # SIG mid - Kalshi mid (adjusted)
    longshot_overpricing: float | None  # SIG ask - Kalshi mid (adjusted), Kalshi < threshold
    flags: list[str] = field(default_factory=list)

    @property
    def abs_gap(self) -> float | None:
        return None if self.gap is None else abs(self.gap)


def build_row(
    cup_row: dict[str, str],
    map_row: dict[str, str],
    sig: SigTop,
    kalshi: KalshiMarket | None,
    event: KalshiEvent | None,
    cfg: LaunchReportConfig,
) -> ReportRow:
    polarity = map_row.get("polarity", "")
    state, party = cup_row["state"], cup_row["party"]
    sig_mid = (sig.bid + sig.ask) / 2 if sig.bid is not None and sig.ask is not None else None
    sig_spread = sig.ask - sig.bid if sig.bid is not None and sig.ask is not None else None
    k_adj = kalshi_mid_in_sig_terms(kalshi, polarity) if kalshi else None
    gap = sig_mid - k_adj if sig_mid is not None and k_adj is not None else None
    longshot = None
    if k_adj is not None and k_adj < cfg.longshot_below and sig.ask is not None:
        longshot = sig.ask - k_adj

    row = ReportRow(
        race_key=cup_row["race_key"],
        state=state,
        office=cup_row["office"],
        party=party,
        sig_market_id=cup_row["id"],
        sig_exchange_id=cup_row["exchange_id"],
        sig_title=cup_row["title"],
        sig_bid=sig.bid,
        sig_bid_size=sig.bid_size,
        sig_ask=sig.ask,
        sig_ask_size=sig.ask_size,
        sig_mid=sig_mid,
        sig_spread=sig_spread,
        kalshi_ticker=map_row.get("kalshi_ticker", ""),
        polarity=polarity,
        kalshi_bid=kalshi.yes_bid if kalshi else None,
        kalshi_ask=kalshi.yes_ask if kalshi else None,
        kalshi_mid_adj=k_adj,
        kalshi_label=kalshi.yes_sub_title if kalshi else "",
        kalshi_party=kalshi_party(kalshi) if kalshi else None,
        kalshi_event_title=event.title if event else "",
        kalshi_volume=kalshi.volume if kalshi else None,
        gap=gap,
        longshot_overpricing=longshot,
    )
    row.flags = row_flags(row, kalshi, event, cfg)
    return row


def row_flags(row: ReportRow, kalshi: KalshiMarket | None, event: KalshiEvent | None, cfg: LaunchReportConfig) -> list[str]:
    flags: list[str] = []
    if row.state in cfg.exclude_states:
        flags.append(f"excluded state {row.state}")
    if cfg.exclude_independent and row.party == "I":
        flags.append("excluded: Independent market")
    if kalshi is None:
        flags.append("Kalshi market not fetched")
        return flags

    if row.polarity == "same":
        if row.kalshi_party is None:
            flags.append("Kalshi party unknown")
        elif row.kalshi_party != row.party:
            flags.append(f"party mismatch: SIG {row.party}, Kalshi {row.kalshi_party}")
    elif row.polarity == "inverted":
        flags.append("inverted polarity: check party by hand")
    else:
        flags.append(f"polarity {row.polarity!r} unknown")

    if row.state != "US":
        title = row.kalshi_event_title or kalshi.title
        title_state = state_in_title(title)
        if title_state != row.state:
            flags.append(f"state mismatch: event title is {title_state or 'no state'}, SIG {row.state}")

    flags += [f"not 2026: {r}" for r in not_2026_reasons(kalshi, event)]

    if row.abs_gap is not None and row.abs_gap > cfg.max_gap:
        flags.append(f"gap {row.gap * 100:+.1f} pts")

    sig_book = _book_state(row.sig_bid, row.sig_ask)
    if sig_book != "two-sided":
        flags.append(f"SIG book {sig_book}")
    k_book = _book_state(row.kalshi_bid, row.kalshi_ask)
    if k_book != "two-sided":
        flags.append(f"Kalshi book {k_book}")

    if row.kalshi_volume is not None and row.kalshi_volume < cfg.min_kalshi_volume:
        flags.append(f"Kalshi volume low ({row.kalshi_volume:,.0f})")
    return flags


def sort_rows(rows: list[ReportRow]) -> list[ReportRow]:
    """Largest absolute gap first; rows with no gap last."""
    return sorted(rows, key=lambda r: (r.abs_gap is None, -(r.abs_gap or 0)))


CSV_COLUMNS = [
    "race_key", "office", "party", "sig_market_id", "sig_exchange_id", "sig_title",
    "sig_bid", "sig_bid_size", "sig_ask", "sig_ask_size", "sig_mid", "sig_spread",
    "kalshi_ticker", "polarity", "kalshi_bid", "kalshi_ask", "kalshi_mid_adj",
    "kalshi_label", "kalshi_party", "kalshi_event_title", "kalshi_volume",
    "gap", "longshot_overpricing", "flags",
]  # fmt: skip


def csv_record(row: ReportRow) -> dict[str, str]:
    def f(v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, float):
            return f"{v:.4f}"
        return str(v)

    rec = {c: f(getattr(row, c)) for c in CSV_COLUMNS if c != "flags"}
    rec["flags"] = "; ".join(row.flags)
    return rec


def _flag_kind(flag: str) -> str:
    """Group flags for the summary ("gap +4.2 pts" -> "gap")."""
    for prefix in ("excluded", "party mismatch", "state mismatch", "not 2026", "gap", "SIG book",
                   "Kalshi book", "Kalshi volume low", "Kalshi party unknown", "Kalshi market not fetched",
                   "inverted polarity", "polarity"):  # fmt: skip
        if flag.startswith(prefix):
            return prefix
    return flag


@dataclass
class Summary:
    clean_races: list[str]
    flagged_races: dict[str, list[str]]  # race_key -> "party: flag" lines
    top_longshots: list[ReportRow]


def summarize(rows: list[ReportRow], top_n: int = 10) -> Summary:
    by_race: dict[str, list[ReportRow]] = {}
    for r in rows:
        by_race.setdefault(r.race_key, []).append(r)
    clean, flagged = [], {}
    for race, rs in sorted(by_race.items()):
        lines = [f"{r.party}: {fl}" for r in sorted(rs, key=lambda r: r.party) for fl in r.flags]
        if lines:
            flagged[race] = lines
        else:
            clean.append(race)
    longshots = sorted(
        (r for r in rows if r.longshot_overpricing is not None),
        key=lambda r: -r.longshot_overpricing,  # type: ignore[operator]
    )[:top_n]
    return Summary(clean_races=clean, flagged_races=flagged, top_longshots=longshots)


def flag_counts(summary: Summary) -> dict[str, int]:
    """How many flagged races carry each kind of flag."""
    counts: dict[str, int] = {}
    for lines in summary.flagged_races.values():
        kinds = {_flag_kind(line.split(": ", 1)[1]) for line in lines}
        for k in kinds:
            counts[k] = counts.get(k, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
