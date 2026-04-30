"""Pure technical analysis calculations — no I/O, no API calls.

All functions take price/volume arrays and return dicts with indicators
and a human-readable signal string.
"""
from __future__ import annotations

from typing import Optional

import numpy as np


def _ema(prices: list[float] | np.ndarray, period: int) -> np.ndarray:
    """Compute exponential moving average."""
    arr = np.asarray(prices, dtype=float)
    alpha = 2.0 / (period + 1)
    ema = np.empty_like(arr)
    ema[0] = arr[0]
    for i in range(1, len(arr)):
        ema[i] = alpha * arr[i] + (1 - alpha) * ema[i - 1]
    return ema


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

def calc_macd(
    prices: list[float],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> Optional[dict]:
    """MACD line, signal line, histogram, and bullish/bearish/neutral signal.

    Returns None if fewer than ``slow + signal_period`` data points.
    """
    min_required = slow + signal_period
    if len(prices) < min_required:
        return None

    fast_ema = _ema(prices, fast)
    slow_ema = _ema(prices, slow)
    macd_line = fast_ema - slow_ema
    signal_line = _ema(macd_line, signal_period)
    histogram = macd_line - signal_line

    current_hist = float(histogram[-1])
    prev_hist = float(histogram[-2])

    if current_hist > 0 and current_hist > prev_hist:
        signal = "bullish"
    elif current_hist < 0 and current_hist < prev_hist:
        signal = "bearish"
    else:
        signal = "neutral"

    return {
        "macd_line": round(float(macd_line[-1]), 4),
        "signal_line": round(float(signal_line[-1]), 4),
        "histogram": round(current_hist, 4),
        "signal": signal,
    }


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

def calc_bollinger_bands(
    prices: list[float],
    period: int = 20,
    num_std: float = 2.0,
) -> Optional[dict]:
    """Upper/lower bands, middle (SMA), %B, and overbought/oversold/neutral.

    Returns None if fewer than ``period`` data points.
    """
    if len(prices) < period:
        return None

    arr = np.asarray(prices, dtype=float)
    window = arr[-period:]
    middle = float(np.mean(window))
    std = float(np.std(window, ddof=1))
    upper = middle + num_std * std
    lower = middle - num_std * std

    current_price = float(arr[-1])
    band_width = upper - lower
    pct_b = (current_price - lower) / band_width if band_width > 0 else 0.5

    if pct_b > 1.0:
        signal = "overbought"
    elif pct_b < 0.0:
        signal = "oversold"
    else:
        signal = "neutral"

    return {
        "upper": round(upper, 4),
        "middle": round(middle, 4),
        "lower": round(lower, 4),
        "pct_b": round(pct_b, 4),
        "signal": signal,
    }


# ---------------------------------------------------------------------------
# 50/200 MA Crossover
# ---------------------------------------------------------------------------

def calc_ma_crossover(prices: list[float]) -> Optional[dict]:
    """50-day and 200-day simple MAs plus crossover signal.

    Signals:
    - ``golden_cross``  — MA50 crossed above MA200 in the last 5 days
    - ``death_cross``   — MA50 crossed below MA200 in the last 5 days
    - ``above_both``    — price above both MAs (no recent cross)
    - ``below_both``    — price below both MAs (no recent cross)
    - ``between``       — price between the two MAs

    Returns None if fewer than 205 data points (200 + 5 for cross detection).
    """
    if len(prices) < 205:
        return None

    arr = np.asarray(prices, dtype=float)

    # Current MAs
    ma_50 = float(np.mean(arr[-50:]))
    ma_200 = float(np.mean(arr[-200:]))

    # 5-day-ago MAs for cross detection
    ma_50_prev = float(np.mean(arr[-55:-5]))
    ma_200_prev = float(np.mean(arr[-205:-5]))

    current_price = float(arr[-1])

    # Detect crossover in the last 5 days
    currently_above = ma_50 > ma_200
    previously_above = ma_50_prev > ma_200_prev

    if currently_above and not previously_above:
        signal = "golden_cross"
    elif not currently_above and previously_above:
        signal = "death_cross"
    elif current_price > ma_50 and current_price > ma_200:
        signal = "above_both"
    elif current_price < ma_50 and current_price < ma_200:
        signal = "below_both"
    else:
        signal = "between"

    return {
        "ma_50": round(ma_50, 4),
        "ma_200": round(ma_200, 4),
        "signal": signal,
    }


# ---------------------------------------------------------------------------
# Average True Range (ATR)
# ---------------------------------------------------------------------------

def calc_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> Optional[dict]:
    """14-day Average True Range for volatility-adjusted stop-loss guidance.

    Returns None if fewer than ``period + 1`` data points.
    """
    if len(closes) < period + 1 or len(highs) < period + 1 or len(lows) < period + 1:
        return None

    true_ranges = []
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        true_ranges.append(max(h - l, abs(h - pc), abs(l - pc)))

    # Use simple average for ATR
    atr_vals = true_ranges[-(period):]
    atr = float(np.mean(atr_vals))
    current_price = closes[-1]
    atr_pct = (atr / current_price * 100) if current_price > 0 else 0.0

    return {
        "atr": round(atr, 4),
        "atr_pct": round(atr_pct, 2),
    }


# ---------------------------------------------------------------------------
# Support / Resistance
# ---------------------------------------------------------------------------

def calc_support_resistance(
    prices: list[float],
    lookback: int = 20,
) -> Optional[dict]:
    """Simple support (min) and resistance (max) over the lookback window.

    Returns None if fewer than ``lookback`` data points.
    """
    if len(prices) < lookback:
        return None

    window = prices[-lookback:]
    return {
        "support": round(float(min(window)), 4),
        "resistance": round(float(max(window)), 4),
    }


# ---------------------------------------------------------------------------
# Volume Profile
# ---------------------------------------------------------------------------

def calc_volume_profile(
    prices: list[float],
    volumes: list[float | int],
    period: int = 20,
) -> Optional[dict]:
    """Average volume, relative volume (latest / avg), and signal.

    Signals: ``high_volume`` (>1.5x), ``low_volume`` (<0.5x), ``normal``.
    Returns None if fewer than ``period`` data points.
    """
    if len(volumes) < period:
        return None

    vol_arr = np.asarray(volumes[-period:], dtype=float)
    avg_vol = float(np.mean(vol_arr))
    latest_vol = float(vol_arr[-1])
    relative = latest_vol / avg_vol if avg_vol > 0 else 1.0

    if relative > 1.5:
        signal = "high_volume"
    elif relative < 0.5:
        signal = "low_volume"
    else:
        signal = "normal"

    return {
        "avg_volume_20d": round(avg_vol, 0),
        "relative_volume": round(relative, 4),
        "signal": signal,
    }


# ---------------------------------------------------------------------------
# GARCH(1,1) Conditional Volatility Forecast
# ---------------------------------------------------------------------------

def forecast_garch_volatility(
    prices: list[float],
    horizon: int = 5,
    history_window: int = 252,
) -> Optional[dict]:
    """Fit GARCH(1,1) on daily log returns and forecast forward volatility.

    GARCH adds a conditional, forward-looking signal that ATR (rolling, lagging)
    cannot give: when vol is mean-reverting up, GARCH expands ahead of ATR; when
    a shock has just hit, GARCH decays back toward unconditional vol while ATR
    stays elevated. This is what makes it useful for sizing decisions before
    the trailing-stop machinery sees the new regime.

    Returns dict with annualized forward vol (%) and a regime signal vs the
    realized 20-day vol; None if fit fails or fewer than 60 returns available.
    """
    if len(prices) < 61:
        return None

    arr = np.asarray(prices[-history_window - 1:], dtype=float)
    if np.any(arr <= 0):
        return None

    # Log returns scaled to percent (arch's preferred numerical range)
    log_returns = np.diff(np.log(arr)) * 100.0
    if len(log_returns) < 60:
        return None

    try:
        from arch import arch_model
    except ImportError:
        return None

    try:
        # Constant mean + GARCH(1,1). Disable optimizer chatter.
        am = arch_model(log_returns, mean="Constant", vol="GARCH", p=1, q=1, dist="normal")
        res = am.fit(disp="off", show_warning=False)
        forecast = res.forecast(horizon=horizon, reindex=False)
        # variance is daily, in (% returns)^2 — annualize to a single % vol
        var_path = forecast.variance.values[-1]
        avg_daily_var = float(np.mean(var_path))
        if avg_daily_var <= 0:
            return None
        forward_daily_vol_pct = float(np.sqrt(avg_daily_var))
        forward_annual_vol_pct = forward_daily_vol_pct * np.sqrt(252)
    except Exception:
        return None

    # Realized 20-day vol (% annualized) from the same series for regime comparison
    recent = log_returns[-20:]
    realized_daily_pct = float(np.std(recent, ddof=1)) if len(recent) > 1 else 0.0
    realized_annual_pct = realized_daily_pct * np.sqrt(252)

    if realized_annual_pct <= 0:
        regime = "unknown"
        ratio = None
    else:
        ratio = forward_annual_vol_pct / realized_annual_pct
        if ratio > 1.15:
            regime = "expanding"
        elif ratio < 0.85:
            regime = "contracting"
        else:
            regime = "stable"

    return {
        "forward_annual_vol_pct": round(forward_annual_vol_pct, 2),
        "forward_daily_vol_pct": round(forward_daily_vol_pct, 2),
        "realized_annual_vol_pct": round(realized_annual_pct, 2),
        "ratio": round(ratio, 2) if ratio is not None else None,
        "horizon_days": horizon,
        "regime": regime,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_technicals(price_data: dict[str, dict]) -> dict[str, dict]:
    """Compute all technicals for multiple symbols.

    Args:
        price_data: ``{symbol: {"history_close": [...], "history_volume": [...],
                        "history_high": [...], "history_low": [...]}}``

    Returns:
        ``{symbol: {"macd": {...}, "bollinger": {...}, "ma_crossover": {...},
                     "support_resistance": {...}, "volume": {...}, "atr": {...},
                     "garch": {...}}}``
    """
    results: dict[str, dict] = {}

    for symbol, data in price_data.items():
        closes = data.get("history_close", [])
        volumes = data.get("history_volume", [])
        highs = data.get("history_high", [])
        lows = data.get("history_low", [])

        results[symbol] = {
            "macd": calc_macd(closes),
            "bollinger": calc_bollinger_bands(closes),
            "ma_crossover": calc_ma_crossover(closes),
            "support_resistance": calc_support_resistance(closes),
            "volume": calc_volume_profile(closes, volumes),
            "atr": calc_atr(highs, lows, closes) if highs and lows else None,
            "garch": forecast_garch_volatility(closes),
        }

    return results
