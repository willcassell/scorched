"""Tests for the drawdown gate pure check function."""
import pytest

from scorched.drawdown_gate import DrawdownGateResult, check_drawdown_gate


DEFAULT_CONFIG = {"enabled": True, "max_drawdown_pct": 8.0}


class TestCheckDrawdownGate:
    def test_no_drawdown_not_blocked(self):
        result = check_drawdown_gate(100_000.0, 100_000.0, DEFAULT_CONFIG)
        assert result.blocked is False
        assert result.current_drawdown_pct == 0.0

    def test_small_drawdown_not_blocked(self):
        # 5% drawdown, threshold is 8%
        result = check_drawdown_gate(100_000.0, 95_000.0, DEFAULT_CONFIG)
        assert result.blocked is False
        assert result.current_drawdown_pct == 5.0

    def test_at_threshold_is_blocked(self):
        # Exactly 8% drawdown
        result = check_drawdown_gate(100_000.0, 92_000.0, DEFAULT_CONFIG)
        assert result.blocked is True
        assert result.current_drawdown_pct == 8.0

    def test_exceeds_threshold_is_blocked(self):
        # 10% drawdown > 8% threshold
        result = check_drawdown_gate(100_000.0, 90_000.0, DEFAULT_CONFIG)
        assert result.blocked is True
        assert result.current_drawdown_pct == 10.0

    def test_portfolio_above_peak_not_blocked(self):
        # Current value above peak — negative drawdown = not blocked
        result = check_drawdown_gate(100_000.0, 105_000.0, DEFAULT_CONFIG)
        assert result.blocked is False
        assert result.current_drawdown_pct < 0

    def test_disabled_never_blocks(self):
        disabled_config = {"enabled": False, "max_drawdown_pct": 8.0}
        result = check_drawdown_gate(100_000.0, 80_000.0, disabled_config)
        assert result.blocked is False

    def test_zero_peak_not_blocked(self):
        result = check_drawdown_gate(0.0, 50_000.0, DEFAULT_CONFIG)
        assert result.blocked is False

    def test_custom_threshold(self):
        config = {"enabled": True, "max_drawdown_pct": 5.0}
        # 6% drawdown > 5% threshold
        result = check_drawdown_gate(100_000.0, 94_000.0, config)
        assert result.blocked is True
        assert result.threshold_pct == 5.0

    def test_result_contains_all_fields(self):
        result = check_drawdown_gate(100_000.0, 95_000.0, DEFAULT_CONFIG)
        assert isinstance(result, DrawdownGateResult)
        assert result.peak_value == 100_000.0
        assert result.current_value == 95_000.0
        assert result.threshold_pct == 8.0
        assert result.current_drawdown_pct == 5.0
        assert result.blocked is False

    def test_missing_config_keys_use_defaults(self):
        # Empty config — should use enabled=True, max_drawdown_pct=8.0
        result = check_drawdown_gate(100_000.0, 91_000.0, {})
        assert result.blocked is True
        assert result.threshold_pct == 8.0
