"""Portfolio-level forward-looking risk metrics.

Computes 1-day Value-at-Risk and Conditional VaR (Expected Shortfall) for the
current book using historical simulation. Returns are weighted by current
position value so the metric reflects the live exposure, not equal weighting.

Hard stops (-8% per position) and the drawdown gate (-8% from peak) are
*reactive* — they only fire after the move. VaR/CVaR are forward-looking
estimates of how bad a single bad day could plausibly be, so they belong on
the dashboard alongside cash floor and concentration.

Method: Historical simulation. Cheap, distribution-free, avoids assumptions
the parametric (delta-normal) method makes that real equity returns violate
(fat tails, skew). Cost is a longer lookback (default 252 trading days).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Portfolio, Position

logger = logging.getLogger(__name__)


@dataclass
class HistoricalSimResult:
    var_pct: float            # 1-day VaR as a *negative* return (e.g. -0.024 = -2.4%)
    cvar_pct: float           # 1-day CVaR (avg of returns at or below VaR)
    var_dollars: float        # |VaR| × portfolio_value (positive number = dollars at risk)
    cvar_dollars: float
    confidence: float         # e.g. 0.95
    lookback_days: int        # number of overlapping return days actually used
    n_positions: int          # number of positions included
    portfolio_value: float    # sum of current market values used for weighting


def historical_var_cvar(
    returns_matrix: np.ndarray,
    weights: Sequence[float],
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Pure historical-simulation VaR and CVaR.

    Args:
        returns_matrix: shape (n_days, n_symbols) of daily simple returns.
        weights: length n_symbols, sum to 1.0 (market-value weights).
        confidence: VaR confidence (0.95 = 5th percentile of P&L distribution).

    Returns (var_pct, cvar_pct), both negative numbers (loss conventions).
    Returns (0.0, 0.0) if the matrix is empty or weights are degenerate.
    """
    if returns_matrix.size == 0 or len(weights) == 0:
        return 0.0, 0.0

    w = np.asarray(weights, dtype=float)
    if w.shape[0] != returns_matrix.shape[1]:
        raise ValueError(
            f"weight length {w.shape[0]} != returns_matrix columns {returns_matrix.shape[1]}"
        )
    if not np.isfinite(w).all() or w.sum() <= 0:
        return 0.0, 0.0
    w = w / w.sum()

    portfolio_returns = returns_matrix @ w  # shape (n_days,)
    if portfolio_returns.size == 0:
        return 0.0, 0.0

    alpha = 1.0 - confidence  # left tail mass
    var = float(np.quantile(portfolio_returns, alpha))
    tail = portfolio_returns[portfolio_returns <= var]
    cvar = float(tail.mean()) if tail.size > 0 else var
    return var, cvar


async def compute_portfolio_risk(
    db: AsyncSession,
    confidence: float = 0.95,
    lookback_days: int = 252,
) -> HistoricalSimResult:
    """Fetch position bars from Alpaca and compute weighted historical VaR/CVaR.

    Cash is treated as risk-free (zero return contribution). If no positions are
    held, the result is all zeros — there's nothing at risk on the equity side.
    """
    portfolio = (await db.execute(select(Portfolio))).scalars().first()
    if portfolio is None:
        raise ValueError("Portfolio not initialized")

    positions = (await db.execute(select(Position))).scalars().all()
    if not positions:
        return HistoricalSimResult(
            var_pct=0.0,
            cvar_pct=0.0,
            var_dollars=0.0,
            cvar_dollars=0.0,
            confidence=confidence,
            lookback_days=0,
            n_positions=0,
            portfolio_value=float(portfolio.cash_balance),
        )

    symbols = [p.symbol for p in positions]
    from .alpaca_data import fetch_bars_sync, fetch_snapshots_sync

    loop = asyncio.get_running_loop()
    bars = await loop.run_in_executor(
        None, lambda: fetch_bars_sync(symbols, days=lookback_days)
    )
    snapshots = await loop.run_in_executor(None, lambda: fetch_snapshots_sync(symbols))

    # Build aligned date index (intersection of all symbols' available dates)
    per_symbol_dates: dict[str, list[str]] = {}
    per_symbol_closes: dict[str, list[float]] = {}
    for sym in symbols:
        sym_bars = bars.get(sym, [])
        if not sym_bars:
            logger.info("VaR: no bars for %s — excluding", sym)
            continue
        per_symbol_dates[sym] = [b["date"] for b in sym_bars]
        per_symbol_closes[sym] = [b["close"] for b in sym_bars]

    if not per_symbol_dates:
        return HistoricalSimResult(
            var_pct=0.0, cvar_pct=0.0, var_dollars=0.0, cvar_dollars=0.0,
            confidence=confidence, lookback_days=0,
            n_positions=0, portfolio_value=float(portfolio.cash_balance),
        )

    common_dates = set.intersection(*(set(d) for d in per_symbol_dates.values()))
    aligned = sorted(common_dates)
    if len(aligned) < 30:
        logger.info("VaR: only %d aligned days — too short for stable VaR", len(aligned))
        return HistoricalSimResult(
            var_pct=0.0, cvar_pct=0.0, var_dollars=0.0, cvar_dollars=0.0,
            confidence=confidence, lookback_days=len(aligned),
            n_positions=len(per_symbol_dates), portfolio_value=float(portfolio.cash_balance),
        )

    # Build (n_days, n_symbols) close-price matrix in aligned order
    sorted_symbols = sorted(per_symbol_dates.keys())
    close_matrix = np.zeros((len(aligned), len(sorted_symbols)), dtype=float)
    for j, sym in enumerate(sorted_symbols):
        date_to_close = dict(zip(per_symbol_dates[sym], per_symbol_closes[sym]))
        close_matrix[:, j] = [date_to_close[d] for d in aligned]

    # Daily simple returns
    returns = np.diff(close_matrix, axis=0) / close_matrix[:-1, :]
    if returns.shape[0] < 20:
        return HistoricalSimResult(
            var_pct=0.0, cvar_pct=0.0, var_dollars=0.0, cvar_dollars=0.0,
            confidence=confidence, lookback_days=int(returns.shape[0]),
            n_positions=len(sorted_symbols), portfolio_value=float(portfolio.cash_balance),
        )

    # Position weights by current market value (cash excluded — cash carries no return risk)
    market_values = []
    for sym in sorted_symbols:
        pos = next(p for p in positions if p.symbol == sym)
        snap = snapshots.get(sym, {})
        live_price = snap.get("current_price")
        if live_price is None or live_price <= 0:
            live_price = float(pos.avg_cost_basis)
        market_values.append(float(pos.shares) * float(live_price))
    total_equity = sum(market_values)
    cash = float(portfolio.cash_balance)
    portfolio_value = total_equity + cash

    if total_equity <= 0:
        return HistoricalSimResult(
            var_pct=0.0, cvar_pct=0.0, var_dollars=0.0, cvar_dollars=0.0,
            confidence=confidence, lookback_days=int(returns.shape[0]),
            n_positions=len(sorted_symbols), portfolio_value=portfolio_value,
        )

    weights = [mv / total_equity for mv in market_values]
    var_pct, cvar_pct = historical_var_cvar(returns, weights, confidence=confidence)

    # VaR is reported as a fraction of *equity*, then translated to dollars on equity.
    # Cash is risk-free (no contribution to either side).
    var_dollars = abs(var_pct) * total_equity
    cvar_dollars = abs(cvar_pct) * total_equity

    return HistoricalSimResult(
        var_pct=var_pct,
        cvar_pct=cvar_pct,
        var_dollars=var_dollars,
        cvar_dollars=cvar_dollars,
        confidence=confidence,
        lookback_days=int(returns.shape[0]),
        n_positions=len(sorted_symbols),
        portfolio_value=portfolio_value,
    )
