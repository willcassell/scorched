"""Tests for portfolio VaR/CVaR (pure function)."""
import numpy as np
import pytest

from scorched.services.risk import historical_var_cvar


class TestHistoricalVarCvar:
    def test_known_distribution(self):
        # Synthetic: single-asset returns drawn from N(0, 1%) over 1000 days.
        # 5th percentile of N(0, 0.01) ≈ -0.0164. CVaR at 95% ≈ -0.0206.
        rng = np.random.default_rng(42)
        returns = rng.normal(loc=0.0, scale=0.01, size=(1000, 1))
        var, cvar = historical_var_cvar(returns, weights=[1.0], confidence=0.95)
        assert -0.025 < var < -0.012
        assert cvar <= var  # tail mean is at or below the quantile

    def test_cvar_strictly_worse_than_var(self):
        rng = np.random.default_rng(7)
        # Multi-asset: 3 symbols, 500 days, mild correlation
        base = rng.normal(0, 0.012, size=(500, 1))
        noise = rng.normal(0, 0.008, size=(500, 3))
        returns = base + noise  # broadcasts to (500, 3)
        var, cvar = historical_var_cvar(returns, weights=[0.5, 0.3, 0.2])
        assert cvar < var
        assert var < 0
        assert cvar < 0

    def test_weights_renormalize(self):
        rng = np.random.default_rng(1)
        returns = rng.normal(0, 0.01, size=(200, 2))
        v1, c1 = historical_var_cvar(returns, weights=[0.5, 0.5])
        v2, c2 = historical_var_cvar(returns, weights=[1.0, 1.0])  # auto-normalized
        assert abs(v1 - v2) < 1e-12
        assert abs(c1 - c2) < 1e-12

    def test_empty_matrix_returns_zeros(self):
        var, cvar = historical_var_cvar(np.zeros((0, 0)), weights=[])
        assert var == 0.0
        assert cvar == 0.0

    def test_weight_length_mismatch_raises(self):
        returns = np.zeros((10, 3))
        with pytest.raises(ValueError):
            historical_var_cvar(returns, weights=[0.5, 0.5])  # 2 vs 3

    def test_zero_weights_returns_zeros(self):
        returns = np.zeros((10, 2))
        # Both zero — degenerate, no exposure
        var, cvar = historical_var_cvar(returns, weights=[0.0, 0.0])
        assert var == 0.0
        assert cvar == 0.0

    def test_all_negative_returns_yields_negative_var(self):
        # Sanity: a guaranteed-loss asset has negative VaR
        returns = -np.abs(np.random.default_rng(3).normal(0, 0.01, size=(500, 1)))
        var, cvar = historical_var_cvar(returns, weights=[1.0])
        assert var < 0
        assert cvar < var

    def test_higher_confidence_means_deeper_var(self):
        rng = np.random.default_rng(9)
        returns = rng.normal(0, 0.015, size=(2000, 1))
        var_95, _ = historical_var_cvar(returns, weights=[1.0], confidence=0.95)
        var_99, _ = historical_var_cvar(returns, weights=[1.0], confidence=0.99)
        # 99% VaR is further into the left tail than 95%
        assert var_99 < var_95
