"""Deterministic risk gate functions, callable from recommender and trade-confirm.

Each function returns a small result dataclass with `passed: bool` plus enough
context to log a precise rejection reason. Pure functions — no DB, no I/O —
so they are cheap to re-run at confirm time.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class CashFloorResult:
    passed: bool
    projected_cash: Decimal
    floor: Decimal
    reason: str = ""


def check_cash_floor(
    current_cash: Decimal,
    total_portfolio_value: Decimal,
    buy_notional: Decimal,
    reserve_pct: Decimal,
) -> CashFloorResult:
    """Return PASS only if `current_cash - buy_notional >= total * reserve_pct`.

    `reserve_pct` is a fraction (0.10 = 10%). `total_portfolio_value` is the
    correct base — using `current_cash` collapses the floor as cash shrinks.
    """
    if total_portfolio_value <= 0:
        return CashFloorResult(
            passed=False,
            projected_cash=Decimal("0"),
            floor=Decimal("0"),
            reason="total_portfolio_value is zero — cannot compute floor",
        )
    floor = (Decimal(str(total_portfolio_value)) * Decimal(str(reserve_pct))).quantize(Decimal("0.01"))
    projected = (Decimal(str(current_cash)) - Decimal(str(buy_notional))).quantize(Decimal("0.01"))
    if projected < floor:
        return CashFloorResult(
            passed=False,
            projected_cash=projected,
            floor=floor,
            reason=f"projected cash ${projected:,.2f} < floor ${floor:,.2f}",
        )
    return CashFloorResult(passed=True, projected_cash=projected, floor=floor)
