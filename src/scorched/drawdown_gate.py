"""Drawdown gate — blocks new buys when portfolio drawdown exceeds threshold.

Pure check function + async DB wrapper. Follows the same pattern as circuit_breaker.py.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Portfolio, Position

logger = logging.getLogger(__name__)


@dataclass
class DrawdownGateResult:
    blocked: bool
    current_drawdown_pct: float
    threshold_pct: float
    peak_value: float
    current_value: float


def check_drawdown_gate(
    peak_value: float,
    current_value: float,
    config: dict,
) -> DrawdownGateResult:
    """Pure function: check if portfolio drawdown exceeds threshold.

    Args:
        peak_value: Historical peak portfolio value.
        current_value: Current total portfolio value (cash + positions at market).
        config: Dict with 'enabled' (bool) and 'max_drawdown_pct' (float).

    Returns:
        DrawdownGateResult with blocked=True if drawdown exceeds threshold.
    """
    enabled = config.get("enabled", True)
    threshold = config.get("max_drawdown_pct", 8.0)

    if not enabled or peak_value <= 0:
        return DrawdownGateResult(
            blocked=False,
            current_drawdown_pct=0.0,
            threshold_pct=threshold,
            peak_value=peak_value,
            current_value=current_value,
        )

    drawdown_pct = ((peak_value - current_value) / peak_value) * 100.0

    blocked = drawdown_pct >= threshold

    return DrawdownGateResult(
        blocked=blocked,
        current_drawdown_pct=round(drawdown_pct, 2),
        threshold_pct=threshold,
        peak_value=peak_value,
        current_value=current_value,
    )


async def update_peak_and_check(
    db: AsyncSession,
    price_data: dict,
    config: dict,
) -> DrawdownGateResult:
    """Load portfolio, compute current value from live prices, update peak, check drawdown.

    Args:
        db: Async database session.
        price_data: Dict keyed by symbol with 'current_price' values.
        config: Drawdown gate config dict ('enabled', 'max_drawdown_pct').

    Returns:
        DrawdownGateResult.
    """
    portfolio = (await db.execute(select(Portfolio))).scalars().first()
    if portfolio is None:
        logger.warning("No portfolio row found — drawdown gate cannot run")
        return DrawdownGateResult(
            blocked=False, current_drawdown_pct=0.0,
            threshold_pct=config.get("max_drawdown_pct", 8.0),
            peak_value=0.0, current_value=0.0,
        )

    positions = (await db.execute(select(Position))).scalars().all()

    # Compute current total value: cash + sum(shares * live_price)
    position_value = Decimal("0")
    for pos in positions:
        live_price = price_data.get(pos.symbol, {}).get("current_price")
        if live_price is not None and live_price > 0:
            position_value += pos.shares * Decimal(str(live_price))
        else:
            # Fall back to cost basis if no live price
            position_value += pos.shares * pos.avg_cost_basis

    current_value = float(portfolio.cash_balance + position_value)

    # Initialize or update peak
    peak = float(portfolio.peak_portfolio_value) if portfolio.peak_portfolio_value is not None else 0.0

    if current_value > peak or peak == 0.0:
        peak = current_value
        portfolio.peak_portfolio_value = Decimal(str(round(peak, 4)))
        await db.flush()
        logger.info("Updated peak portfolio value to $%.2f", peak)

    result = check_drawdown_gate(peak, current_value, config)

    if result.blocked:
        logger.warning(
            "DRAWDOWN GATE TRIGGERED: portfolio down %.1f%% from peak ($%.2f → $%.2f), "
            "threshold %.1f%% — blocking new buys",
            result.current_drawdown_pct, result.peak_value, result.current_value,
            result.threshold_pct,
        )

    return result
