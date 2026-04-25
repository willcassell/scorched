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


@dataclass
class BuyGatesResult:
    passed: bool
    reason: str = ""
    details: dict | None = None


def run_all_buy_gates(
    *,
    symbol: str,
    sector: str | None,
    buy_notional: Decimal,
    current_cash: Decimal,
    total_portfolio_value: Decimal,
    held_symbols: set[str],
    held_positions_with_sector: list[dict],
    existing_position_value: Decimal,
    reserve_pct: Decimal,
    max_position_pct: Decimal,
    max_sector_pct: float,
    max_holdings: int,
) -> BuyGatesResult:
    """Run cash floor + holdings + position cap + sector gates in one shot.

    Pure function — no DB, no I/O. Safe to call at confirm time as a
    re-validation of the original recommendation gates.
    """
    cash = check_cash_floor(current_cash, total_portfolio_value, buy_notional, reserve_pct)
    if not cash.passed:
        return BuyGatesResult(passed=False, reason=f"cash_floor: {cash.reason}", details={"cash": cash.__dict__})

    holdings = check_holdings_cap(held_symbols, set(), symbol, max_holdings)
    if not holdings.passed:
        return BuyGatesResult(passed=False, reason=f"holdings: {holdings.reason}", details={"holdings": holdings.__dict__})

    pos = check_position_cap(existing_position_value, buy_notional, total_portfolio_value, max_position_pct)
    if not pos.passed:
        return BuyGatesResult(passed=False, reason=f"position_cap: {pos.reason}", details={"position": pos.__dict__})

    # Sector check lives in recommender; import locally to avoid circular import.
    from .services.recommender import check_sector_exposure
    sector_ok = check_sector_exposure(
        proposed_symbol=symbol,
        proposed_sector=sector,
        proposed_dollars=buy_notional,
        held_positions=held_positions_with_sector,
        total_value=total_portfolio_value,
        max_sector_pct=max_sector_pct,
    )
    if not sector_ok:
        return BuyGatesResult(passed=False, reason="sector_cap: would breach sector concentration limit", details=None)

    return BuyGatesResult(passed=True)
