"""Tests for circuit breaker gate checks."""
import pytest
from decimal import Decimal
from unittest.mock import patch, AsyncMock

from scorched.circuit_breaker import (
    check_stock_gate,
    check_market_gate,
    run_circuit_breaker,
)


CB_CONFIG = {
    "enabled": True,
    "stock_gap_down_pct": 2.0,
    "stock_price_drift_pct": 1.5,
    "spy_gap_down_pct": 1.0,
    "vix_absolute_max": 30,
    "vix_spike_pct": 20.0,
}


class TestStockGate:
    def test_passes_when_price_stable(self):
        result = check_stock_gate(
            symbol="AAPL",
            suggested_price=Decimal("150.00"),
            current_price=Decimal("149.50"),
            prior_close=Decimal("150.00"),
            config=CB_CONFIG,
        )
        assert result.passed is True

    def test_fails_on_gap_down_from_close(self):
        # 3.33% gap down > 2% threshold
        result = check_stock_gate(
            symbol="AAPL",
            suggested_price=Decimal("150.00"),
            current_price=Decimal("145.00"),
            prior_close=Decimal("150.00"),
            config=CB_CONFIG,
        )
        assert result.passed is False
        assert "gap_down" in result.reason

    def test_fails_on_drift_from_suggested(self):
        # 2% drift > 1.5% threshold
        result = check_stock_gate(
            symbol="AAPL",
            suggested_price=Decimal("150.00"),
            current_price=Decimal("147.00"),
            prior_close=Decimal("149.00"),
            config=CB_CONFIG,
        )
        assert result.passed is False
        assert "drift" in result.reason


class TestMarketGate:
    def test_passes_when_market_calm(self):
        result = check_market_gate(
            spy_current=Decimal("500.00"),
            spy_prior_close=Decimal("501.00"),
            vix_current=Decimal("18.00"),
            vix_prior_close=Decimal("17.00"),
            config=CB_CONFIG,
        )
        assert result.passed is True

    def test_fails_on_spy_gap_down(self):
        # SPY down 1.5% > 1% threshold
        result = check_market_gate(
            spy_current=Decimal("492.50"),
            spy_prior_close=Decimal("500.00"),
            vix_current=Decimal("18.00"),
            vix_prior_close=Decimal("17.00"),
            config=CB_CONFIG,
        )
        assert result.passed is False
        assert "SPY" in result.reason

    def test_fails_on_vix_absolute(self):
        result = check_market_gate(
            spy_current=Decimal("499.00"),
            spy_prior_close=Decimal("500.00"),
            vix_current=Decimal("32.00"),
            vix_prior_close=Decimal("28.00"),
            config=CB_CONFIG,
        )
        assert result.passed is False
        assert "VIX" in result.reason

    def test_fails_on_vix_spike(self):
        # VIX jumped 25% > 20% threshold
        result = check_market_gate(
            spy_current=Decimal("499.00"),
            spy_prior_close=Decimal("500.00"),
            vix_current=Decimal("25.00"),
            vix_prior_close=Decimal("20.00"),
            config=CB_CONFIG,
        )
        assert result.passed is False
        assert "VIX" in result.reason

    def test_disabled_always_passes(self):
        disabled = {**CB_CONFIG, "enabled": False}
        result = check_market_gate(
            spy_current=Decimal("400.00"),
            spy_prior_close=Decimal("500.00"),
            vix_current=Decimal("50.00"),
            vix_prior_close=Decimal("20.00"),
            config=disabled,
        )
        assert result.passed is True
