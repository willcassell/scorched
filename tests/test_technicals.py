"""Tests for technical analysis calculations."""
import pytest
import numpy as np
from scorched.services.technicals import (
    calc_macd,
    calc_bollinger_bands,
    calc_ma_crossover,
    calc_support_resistance,
    calc_volume_profile,
    compute_technicals,
    forecast_garch_volatility,
)


def _make_prices(n=60, start=100.0, trend=0.5, noise=2.0):
    """Generate synthetic price series for testing."""
    np.random.seed(42)
    prices = [start]
    for _ in range(n - 1):
        prices.append(prices[-1] + trend + np.random.normal(0, noise))
    return prices


class TestMACD:
    def test_returns_correct_keys(self):
        prices = _make_prices(60)
        result = calc_macd(prices)
        assert "macd_line" in result
        assert "signal_line" in result
        assert "histogram" in result
        assert "signal" in result

    def test_signal_is_valid_enum(self):
        prices = _make_prices(60)
        result = calc_macd(prices)
        assert result["signal"] in ("bullish", "bearish", "neutral")

    def test_insufficient_data_returns_none(self):
        result = calc_macd([100, 101, 102])
        assert result is None


class TestBollingerBands:
    def test_returns_correct_keys(self):
        prices = _make_prices(30)
        result = calc_bollinger_bands(prices)
        assert "upper" in result
        assert "middle" in result
        assert "lower" in result
        assert "pct_b" in result
        assert "signal" in result

    def test_price_within_bands(self):
        prices = _make_prices(30)
        result = calc_bollinger_bands(prices)
        assert result["lower"] <= result["middle"] <= result["upper"]

    def test_signal_is_valid(self):
        prices = _make_prices(30)
        result = calc_bollinger_bands(prices)
        assert result["signal"] in ("overbought", "oversold", "neutral")


class TestMACrossover:
    def test_returns_correct_keys(self):
        prices = _make_prices(210)
        result = calc_ma_crossover(prices)
        assert "ma_50" in result
        assert "ma_200" in result
        assert "signal" in result

    def test_signal_values(self):
        prices = _make_prices(210)
        result = calc_ma_crossover(prices)
        assert result["signal"] in ("golden_cross", "death_cross", "above_both", "below_both", "between")


class TestSupportResistance:
    def test_returns_levels(self):
        prices = _make_prices(60)
        result = calc_support_resistance(prices)
        assert "support" in result
        assert "resistance" in result
        assert isinstance(result["support"], float)
        assert isinstance(result["resistance"], float)
        assert result["support"] < result["resistance"]


class TestVolumeProfile:
    def test_returns_signal(self):
        prices = _make_prices(20)
        volumes = [1_000_000 + i * 50_000 for i in range(20)]
        result = calc_volume_profile(prices, volumes)
        assert "avg_volume_20d" in result
        assert "relative_volume" in result
        assert "signal" in result
        assert result["signal"] in ("high_volume", "low_volume", "normal")


class TestComputeTechnicals:
    def test_returns_dict_per_symbol(self):
        price_data = {
            "AAPL": {
                "history_close": _make_prices(210),
                "history_volume": [1_000_000] * 210,
            }
        }
        result = compute_technicals(price_data)
        assert "AAPL" in result
        assert "macd" in result["AAPL"]
        assert "bollinger" in result["AAPL"]
        assert "ma_crossover" in result["AAPL"]
        assert "support_resistance" in result["AAPL"]
        assert "volume" in result["AAPL"]


class TestGarchForecast:
    def test_returns_required_keys_for_normal_series(self):
        prices = _make_prices(260, trend=0.1, noise=1.5)
        result = forecast_garch_volatility(prices)
        assert result is not None
        assert "forward_annual_vol_pct" in result
        assert "forward_daily_vol_pct" in result
        assert "realized_annual_vol_pct" in result
        assert "regime" in result
        assert result["regime"] in ("expanding", "contracting", "stable", "unknown")
        assert result["horizon_days"] == 5

    def test_short_history_returns_none(self):
        # Below 60-return floor (~61 prices)
        result = forecast_garch_volatility([100.0 + i * 0.1 for i in range(40)])
        assert result is None

    def test_zero_or_negative_prices_returns_none(self):
        result = forecast_garch_volatility([0.0] * 100)
        assert result is None

    def test_forecast_is_positive_for_volatile_series(self):
        prices = _make_prices(260, trend=0.0, noise=3.0)
        result = forecast_garch_volatility(prices)
        assert result is not None
        assert result["forward_annual_vol_pct"] > 0
        assert result["forward_daily_vol_pct"] > 0
