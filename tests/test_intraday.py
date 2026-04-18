"""Tests for intraday trigger check functions."""

from decimal import Decimal

from scorched.circuit_breaker import GateResult
from scorched.intraday import (
    check_intraday_triggers,
    check_market_triggers,
    check_position_drop_from_entry,
    check_position_drop_from_open,
    check_spy_intraday_drop,
    check_trailing_stop_breach,
    check_vix_level,
    check_volume_surge,
)


class TestPositionDropFromEntry:
    def test_fires_when_exceeds_threshold(self):
        result = check_position_drop_from_entry(Decimal("94"), Decimal("100"), 5.0)
        assert result.passed is False
        assert "6.0%" in result.reason
        assert "threshold: 5.0%" in result.reason

    def test_passes_when_below_threshold(self):
        result = check_position_drop_from_entry(Decimal("96"), Decimal("100"), 5.0)
        assert result.passed is True

    def test_passes_when_price_is_up(self):
        result = check_position_drop_from_entry(Decimal("105"), Decimal("100"), 5.0)
        assert result.passed is True

    def test_handles_zero_entry(self):
        result = check_position_drop_from_entry(Decimal("50"), Decimal("0"), 5.0)
        assert result.passed is True

    def test_fires_at_exact_threshold(self):
        # 5% drop exactly: (100-95)/100*100 = 5.0, not > 5.0
        result = check_position_drop_from_entry(Decimal("95"), Decimal("100"), 5.0)
        assert result.passed is True

    def test_fires_just_above_threshold(self):
        result = check_position_drop_from_entry(Decimal("94.99"), Decimal("100"), 5.0)
        assert result.passed is False


class TestPositionDropFromOpen:
    def test_fires_when_exceeds_threshold(self):
        result = check_position_drop_from_open(Decimal("96.5"), Decimal("100"), 3.0)
        assert result.passed is False
        assert "3.5%" in result.reason
        assert "threshold: 3.0%" in result.reason

    def test_passes_when_below_threshold(self):
        result = check_position_drop_from_open(Decimal("98"), Decimal("100"), 3.0)
        assert result.passed is True

    def test_handles_zero_open(self):
        result = check_position_drop_from_open(Decimal("50"), Decimal("0"), 3.0)
        assert result.passed is True


class TestSpyIntradayDrop:
    def test_fires_on_drop(self):
        result = check_spy_intraday_drop(Decimal("487.5"), Decimal("500"), 2.0)
        assert result.passed is False
        assert "SPY" in result.reason
        assert "2.5%" in result.reason

    def test_passes_when_stable(self):
        result = check_spy_intraday_drop(Decimal("499"), Decimal("500"), 2.0)
        assert result.passed is True

    def test_handles_zero_open(self):
        result = check_spy_intraday_drop(Decimal("400"), Decimal("0"), 2.0)
        assert result.passed is True


class TestVixLevel:
    def test_fires_when_high(self):
        result = check_vix_level(Decimal("35"), 30.0)
        assert result.passed is False
        assert "35.0" in result.reason
        assert "30" in result.reason

    def test_passes_when_normal(self):
        result = check_vix_level(Decimal("20"), 30.0)
        assert result.passed is True


class TestVolumeSurge:
    def test_fires_on_spike(self):
        result = check_volume_surge(15_000_000, 4_000_000, 3.0)
        assert result.passed is False
        assert "3.8x" in result.reason or "3.7x" in result.reason
        assert "threshold: 3.0x" in result.reason

    def test_passes_on_normal(self):
        result = check_volume_surge(5_000_000, 4_000_000, 3.0)
        assert result.passed is True

    def test_handles_zero_avg(self):
        result = check_volume_surge(5_000_000, 0, 3.0)
        assert result.passed is True


class TestCheckMarketTriggers:
    def test_returns_fired_results(self):
        # check_market_triggers only gates on SPY drop; VIX is passed to Claude
        # as context but intentionally excluded as a trigger (avoids repeated
        # VIX alerts for every held position on high-vol days).
        results = check_market_triggers(
            spy_current=Decimal("487.5"),
            spy_open=Decimal("500"),
            vix_current=Decimal("35"),
            config={"spy_intraday_drop_pct": 2.0, "vix_absolute_max": 30},
        )
        assert len(results) == 1
        assert results[0].passed is False
        assert "SPY" in results[0].reason

    def test_returns_empty_when_all_pass(self):
        results = check_market_triggers(
            spy_current=Decimal("499"),
            spy_open=Decimal("500"),
            vix_current=Decimal("20"),
            config={},
        )
        assert results == []

    def test_uses_defaults(self):
        # SPY down 1.5% (below default 2.0), VIX at 25 (below default 30)
        results = check_market_triggers(
            spy_current=Decimal("492.5"),
            spy_open=Decimal("500"),
            vix_current=Decimal("25"),
            config={},
        )
        assert results == []


class TestCheckIntradayTriggers:
    def test_combines_position_and_market_triggers(self):
        market = [GateResult(passed=False, reason="SPY down")]
        results = check_intraday_triggers(
            current_price=Decimal("90"),
            entry_price=Decimal("100"),
            today_open=Decimal("100"),
            current_volume=20_000_000,
            avg_volume_20d=5_000_000,
            market_triggers=market,
            config={"position_drop_from_entry_pct": 5.0, "position_drop_from_open_pct": 3.0, "volume_surge_multiplier": 3.0},
        )
        # entry drop (10%), open drop (10%), volume surge (4x), + 1 market trigger
        assert len(results) == 4

    def test_includes_market_triggers(self):
        market = [GateResult(passed=False, reason="VIX high")]
        results = check_intraday_triggers(
            current_price=Decimal("100"),
            entry_price=Decimal("100"),
            today_open=Decimal("100"),
            current_volume=1_000_000,
            avg_volume_20d=1_000_000,
            market_triggers=market,
            config={},
        )
        assert len(results) == 1
        assert results[0].reason == "VIX high"

    def test_empty_when_nothing_triggers(self):
        results = check_intraday_triggers(
            current_price=Decimal("100"),
            entry_price=Decimal("100"),
            today_open=Decimal("100"),
            current_volume=1_000_000,
            avg_volume_20d=1_000_000,
            market_triggers=[],
            config={},
        )
        assert results == []

    def test_trailing_stop_breach_fires_when_price_below_stop(self):
        """Price below trailing stop must produce a trailing_stop trigger reason."""
        results = check_intraday_triggers(
            current_price=Decimal("108"),   # below trailing stop of 110
            entry_price=Decimal("100"),
            today_open=Decimal("112"),
            current_volume=1_000_000,
            avg_volume_20d=1_000_000,
            market_triggers=[],
            # high thresholds so other triggers don't fire
            config={
                "position_drop_from_entry_pct": 20.0,
                "position_drop_from_open_pct": 20.0,
                "volume_surge_multiplier": 10.0,
            },
            trailing_stop_price=Decimal("110"),
        )
        assert len(results) == 1
        assert "Trailing stop" in results[0].reason
        assert results[0].passed is False

    def test_trailing_stop_no_trigger_when_above_stop(self):
        """Price above trailing stop must not fire."""
        results = check_intraday_triggers(
            current_price=Decimal("112"),   # above trailing stop of 110
            entry_price=Decimal("100"),
            today_open=Decimal("111"),
            current_volume=1_000_000,
            avg_volume_20d=1_000_000,
            market_triggers=[],
            config={
                "position_drop_from_entry_pct": 20.0,
                "position_drop_from_open_pct": 20.0,
                "volume_surge_multiplier": 10.0,
            },
            trailing_stop_price=Decimal("110"),
        )
        assert results == []

    def test_trailing_stop_none_does_not_fire(self):
        """Omitting trailing_stop_price (None) must not produce a trigger."""
        results = check_intraday_triggers(
            current_price=Decimal("100"),
            entry_price=Decimal("100"),
            today_open=Decimal("100"),
            current_volume=1_000_000,
            avg_volume_20d=1_000_000,
            market_triggers=[],
            config={},
            trailing_stop_price=None,
        )
        assert results == []


class TestCheckTrailingStopBreach:
    def test_fires_when_price_below_stop(self):
        result = check_trailing_stop_breach(Decimal("94"), Decimal("95"))
        assert result.passed is False
        assert "94" in result.reason
        assert "95" in result.reason

    def test_passes_when_price_equals_stop(self):
        # Exactly at stop — not yet breached (< not <=)
        result = check_trailing_stop_breach(Decimal("95"), Decimal("95"))
        assert result.passed is True

    def test_passes_when_price_above_stop(self):
        result = check_trailing_stop_breach(Decimal("100"), Decimal("95"))
        assert result.passed is True

    def test_passes_when_stop_is_none(self):
        result = check_trailing_stop_breach(Decimal("50"), None)
        assert result.passed is True
