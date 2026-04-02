"""Tests for correlation analysis with known price series."""
import pytest
import numpy as np

from scorched.correlation import compute_pairwise_correlations, find_high_correlations


def _make_price_series(returns: list[float], start: float = 100.0) -> list[float]:
    """Build a price series from daily returns (as fractions, e.g. 0.01 = +1%)."""
    prices = [start]
    for r in returns:
        prices.append(prices[-1] * (1 + r))
    return prices


class TestComputePairwiseCorrelations:
    def test_perfectly_correlated(self):
        # Same returns → correlation = 1.0
        returns = [0.01, -0.02, 0.015, -0.005, 0.03] * 5  # 25 days
        price_data = {
            "AAA": {"history_close": _make_price_series(returns)},
            "BBB": {"history_close": _make_price_series(returns)},
        }
        corrs = compute_pairwise_correlations("AAA", ["BBB"], price_data, lookback=20)
        assert "BBB" in corrs
        assert corrs["BBB"] == pytest.approx(1.0, abs=0.01)

    def test_perfectly_anticorrelated(self):
        # Opposite returns → correlation = -1.0
        returns = [0.01, -0.02, 0.015, -0.005, 0.03] * 5
        opposite = [-r for r in returns]
        price_data = {
            "AAA": {"history_close": _make_price_series(returns)},
            "BBB": {"history_close": _make_price_series(opposite)},
        }
        corrs = compute_pairwise_correlations("AAA", ["BBB"], price_data, lookback=20)
        assert "BBB" in corrs
        assert corrs["BBB"] == pytest.approx(-1.0, abs=0.01)

    def test_uncorrelated_random(self):
        # Random, independent series — correlation should be near zero (within tolerance)
        np.random.seed(42)
        returns_a = np.random.normal(0, 0.02, 100).tolist()
        returns_b = np.random.normal(0, 0.02, 100).tolist()
        price_data = {
            "AAA": {"history_close": _make_price_series(returns_a)},
            "BBB": {"history_close": _make_price_series(returns_b)},
        }
        corrs = compute_pairwise_correlations("AAA", ["BBB"], price_data, lookback=20)
        assert "BBB" in corrs
        assert abs(corrs["BBB"]) < 0.5  # not highly correlated

    def test_insufficient_data_returns_empty(self):
        # Only 5 prices (4 returns) < lookback=20
        price_data = {
            "AAA": {"history_close": [100, 101, 102, 103, 104]},
            "BBB": {"history_close": [100, 101, 102, 103, 104]},
        }
        corrs = compute_pairwise_correlations("AAA", ["BBB"], price_data, lookback=20)
        assert corrs == {}

    def test_missing_symbol_returns_empty(self):
        returns = [0.01] * 25
        price_data = {
            "AAA": {"history_close": _make_price_series(returns)},
        }
        corrs = compute_pairwise_correlations("AAA", ["BBB"], price_data, lookback=20)
        assert corrs == {}

    def test_candidate_missing_returns_empty(self):
        returns = [0.01] * 25
        price_data = {
            "BBB": {"history_close": _make_price_series(returns)},
        }
        corrs = compute_pairwise_correlations("AAA", ["BBB"], price_data, lookback=20)
        assert corrs == {}

    def test_skips_self_correlation(self):
        returns = [0.01] * 25
        price_data = {
            "AAA": {"history_close": _make_price_series(returns)},
        }
        corrs = compute_pairwise_correlations("AAA", ["AAA"], price_data, lookback=20)
        assert "AAA" not in corrs

    def test_multiple_held_symbols(self):
        returns = [0.01, -0.02, 0.015, -0.005, 0.03] * 5
        price_data = {
            "AAA": {"history_close": _make_price_series(returns)},
            "BBB": {"history_close": _make_price_series(returns)},
            "CCC": {"history_close": _make_price_series([-r for r in returns])},
        }
        corrs = compute_pairwise_correlations("AAA", ["BBB", "CCC"], price_data, lookback=20)
        assert len(corrs) == 2
        assert corrs["BBB"] > 0.9
        assert corrs["CCC"] < -0.9


class TestFindHighCorrelations:
    def test_returns_only_above_threshold(self):
        returns = [0.01, -0.02, 0.015, -0.005, 0.03] * 5
        price_data = {
            "AAA": {"history_close": _make_price_series(returns)},
            "BBB": {"history_close": _make_price_series(returns)},
            "CCC": {"history_close": _make_price_series([-r for r in returns])},
        }
        high = find_high_correlations("AAA", ["BBB", "CCC"], price_data, threshold=0.8)
        assert len(high) == 1
        assert high[0]["symbol"] == "BBB"
        assert high[0]["correlation"] > 0.9

    def test_empty_when_below_threshold(self):
        np.random.seed(42)
        returns_a = np.random.normal(0, 0.02, 25).tolist()
        returns_b = np.random.normal(0, 0.02, 25).tolist()
        price_data = {
            "AAA": {"history_close": _make_price_series(returns_a)},
            "BBB": {"history_close": _make_price_series(returns_b)},
        }
        high = find_high_correlations("AAA", ["BBB"], price_data, threshold=0.8)
        assert high == []

    def test_no_held_symbols(self):
        returns = [0.01] * 25
        price_data = {"AAA": {"history_close": _make_price_series(returns)}}
        high = find_high_correlations("AAA", [], price_data)
        assert high == []

    def test_sorted_by_correlation_descending(self):
        # Create three held symbols with varying correlations
        base = [0.01, -0.02, 0.015, -0.005, 0.03] * 5
        price_data = {
            "CAND": {"history_close": _make_price_series(base)},
            "HIGH": {"history_close": _make_price_series(base)},              # r ~ 1.0
            "MED":  {"history_close": _make_price_series([r * 0.8 + 0.001 for r in base])},  # r ~ high
        }
        high = find_high_correlations("CAND", ["HIGH", "MED"], price_data, threshold=0.5)
        if len(high) >= 2:
            assert high[0]["correlation"] >= high[1]["correlation"]
