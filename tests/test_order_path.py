"""CLAUDE.md hard rule 1, structurally: no code outside the order router
(and venue implementations themselves) calls place_order or place_batch.
Tests are exempt: they exercise venues directly."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {"predcup/orders.py"}
ALLOWED_DIRS = ("predcup/venues/", "sim/")  # a venue's own internals (e.g. default place_batch)
ORDER_METHODS = {"place_order", "place_batch"}


def _calls(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        (node.lineno, node.func.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ORDER_METHODS
    ]


def test_only_the_order_router_places_orders():
    offenders = []
    for base in ("predcup", "scripts", "sim", "dashboard"):
        for path in (ROOT / base).rglob("*.py") if (ROOT / base).exists() else []:
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOWED or rel.startswith(ALLOWED_DIRS):
                continue
            offenders += [f"{rel}:{line} calls {name}" for line, name in _calls(path)]
    assert offenders == [], "orders must go through predcup.orders.OrderRouter (risk.check first):\n" + "\n".join(offenders)


def test_router_calls_risk_check_before_placing():
    src = (ROOT / "predcup/orders.py").read_text()
    assert src.index("self._risk.check(") < src.index(".place_batch(")


def test_tournament_wide_cancel_all_only_in_kill_switch_and_shutdown():
    """The re-quote path cancels by exchange; a scope-less cancel_all(tid)
    appears only in RiskManager.kill_switch (used by kill and shutdown)."""
    offenders = []
    for base in ("predcup", "scripts", "sim"):
        for path in (ROOT / base).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith(ALLOWED_DIRS):
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "cancel_all":
                    scoped = any(k.arg in ("exchange_id", "market_id") for k in node.keywords) or len(node.args) > 1
                    if not scoped and rel != "predcup/risk.py":
                        offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], offenders
