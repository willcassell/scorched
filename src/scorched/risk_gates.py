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


@dataclass
class HoldingsCapResult:
    passed: bool
    projected_count: int
    cap: int
    reason: str = ""


def check_holdings_cap(
    held_symbols: set[str],
    accepted_new_symbols: set[str],
    proposed_symbol: str,
    max_holdings: int,
) -> HoldingsCapResult:
    """Return PASS unless adding `proposed_symbol` would exceed `max_holdings`.

    Adding to an *existing* holding does not increase the count. Only buys of
    new symbols not already in `held_symbols` or `accepted_new_symbols` count.
    """
    proposed = proposed_symbol.upper()
    held_upper = {s.upper() for s in held_symbols}
    accepted_upper = {s.upper() for s in accepted_new_symbols}

    if proposed in held_upper or proposed in accepted_upper:
        return HoldingsCapResult(
            passed=True,
            projected_count=len(held_upper | accepted_upper),
            cap=max_holdings,
            reason="add to existing holding — does not increase count",
        )

    projected = len(held_upper | accepted_upper) + 1
    if projected > max_holdings:
        return HoldingsCapResult(
            passed=False,
            projected_count=projected,
            cap=max_holdings,
            reason=f"would create holding #{projected} > cap {max_holdings}",
        )
    return HoldingsCapResult(passed=True, projected_count=projected, cap=max_holdings)


@dataclass
class PositionCapResult:
    passed: bool
    projected_pct: float
    cap_pct: float
    reason: str = ""


def check_position_cap(
    existing_market_value: Decimal,
    buy_notional: Decimal,
    total_portfolio_value: Decimal,
    max_position_pct: Decimal,
) -> PositionCapResult:
    """Reject if `(existing + buy) / total * 100 > max_position_pct`."""
    if total_portfolio_value <= 0:
        return PositionCapResult(
            passed=False,
            projected_pct=0.0,
            cap_pct=float(max_position_pct),
            reason="total_portfolio_value is zero",
        )
    post_trade_value = Decimal(str(existing_market_value)) + Decimal(str(buy_notional))
    pct = float(post_trade_value) / float(total_portfolio_value) * 100
    cap = float(max_position_pct)
    if pct > cap:
        return PositionCapResult(
            passed=False,
            projected_pct=pct,
            cap_pct=cap,
            reason=f"post-trade exposure {pct:.1f}% > cap {cap:.1f}%",
        )
    return PositionCapResult(passed=True, projected_pct=pct, cap_pct=cap)
