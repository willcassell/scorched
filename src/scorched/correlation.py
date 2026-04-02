"""Correlation analysis — detect highly correlated positions before buying.

Pure functions using numpy. Uses daily returns (not prices) for statistical correctness.
"""
from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


def compute_pairwise_correlations(
    candidate: str,
    held_symbols: list[str],
    price_data: dict,
    lookback: int = 20,
) -> dict[str, float]:
    """Compute correlation between candidate and each held symbol using daily returns.

    Args:
        candidate: Symbol being considered for purchase.
        held_symbols: Symbols currently held in portfolio.
        price_data: Dict keyed by symbol with 'history_close' arrays.
        lookback: Number of trading days for correlation window.

    Returns:
        Dict mapping held symbol to Pearson correlation coefficient.
        Symbols with insufficient data are omitted.
    """
    candidate_closes = price_data.get(candidate, {}).get("history_close")
    if candidate_closes is None or len(candidate_closes) < lookback + 1:
        return {}

    candidate_prices = np.asarray(candidate_closes[-(lookback + 1):], dtype=float)
    candidate_returns = np.diff(candidate_prices) / candidate_prices[:-1]

    correlations = {}
    for symbol in held_symbols:
        if symbol == candidate:
            continue
        held_closes = price_data.get(symbol, {}).get("history_close")
        if held_closes is None or len(held_closes) < lookback + 1:
            continue

        held_prices = np.asarray(held_closes[-(lookback + 1):], dtype=float)
        held_returns = np.diff(held_prices) / held_prices[:-1]

        # Both return arrays should be the same length (lookback days)
        min_len = min(len(candidate_returns), len(held_returns))
        if min_len < 5:
            continue

        corr_matrix = np.corrcoef(candidate_returns[:min_len], held_returns[:min_len])
        corr = float(corr_matrix[0, 1])

        if not np.isnan(corr):
            correlations[symbol] = round(corr, 3)

    return correlations


def find_high_correlations(
    candidate: str,
    held_symbols: list[str],
    price_data: dict,
    threshold: float = 0.8,
    lookback: int = 20,
) -> list[dict]:
    """Return held symbols with correlation above threshold.

    Args:
        candidate: Symbol being considered for purchase.
        held_symbols: Symbols currently held in portfolio.
        price_data: Dict keyed by symbol with 'history_close' arrays.
        threshold: Correlation threshold (default 0.8).
        lookback: Number of trading days for correlation window.

    Returns:
        List of {'symbol': str, 'correlation': float} dicts for symbols above threshold.
    """
    all_corrs = compute_pairwise_correlations(candidate, held_symbols, price_data, lookback)

    high = [
        {"symbol": sym, "correlation": corr}
        for sym, corr in all_corrs.items()
        if corr >= threshold
    ]

    # Sort by correlation descending
    high.sort(key=lambda x: x["correlation"], reverse=True)

    if high:
        logger.info(
            "High correlation for %s: %s",
            candidate,
            ", ".join(f"{h['symbol']}={h['correlation']:.2f}" for h in high),
        )

    return high
