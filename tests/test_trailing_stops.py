"""Tests for ATR-based trailing stop logic."""

import logging
from decimal import Decimal

from scorched.trailing_stops import compute_trailing_stop, update_trailing_stop


class TestInitialStop:
    """Entry price only, no ATR data."""

    def test_initial_stop_no_atr(self):
        result = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("100"),
            high_water_mark=None,
            atr=None,
        )
        assert result["trailing_stop_price"] == Decimal("95.0000")
        assert result["high_water_mark"] == Decimal("100")
        assert result["stop_type"] == "fixed_pct"

    def test_initial_stop_custom_pct(self):
        result = compute_trailing_stop(
            entry_price=Decimal("200"),
            current_price=Decimal("200"),
            high_water_mark=None,
            atr=None,
            min_stop_pct=3.0,
        )
        assert result["trailing_stop_price"] == Decimal("194.0000")

    def test_hwm_initialized_to_max_of_entry_and_current(self):
        result = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("110"),
            high_water_mark=None,
            atr=None,
        )
        assert result["high_water_mark"] == Decimal("110")


class TestATRStop:
    """ATR-based stop should be tighter than fixed % for low-volatility stocks."""

    def test_atr_stop_tighter_than_fixed(self):
        # Low-vol stock: ATR = $1.50, so 2x ATR stop = HWM - $3 = $107
        # Fixed 5% stop = $100 * 0.95 = $95
        # ATR stop ($107) is higher/tighter — should be used
        result = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("110"),
            high_water_mark=Decimal("110"),
            atr=1.5,
            atr_multiplier=2.0,
        )
        assert result["trailing_stop_price"] == Decimal("107.0000")
        assert result["stop_type"] == "atr"

    def test_fixed_used_when_atr_stop_is_looser(self):
        # High-vol stock: ATR = $10, so 2x ATR stop = $100 - $20 = $80
        # Fixed 5% stop = $100 * 0.95 = $95
        # Fixed stop ($95) is higher/tighter — should be used
        result = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("100"),
            high_water_mark=Decimal("100"),
            atr=10.0,
            atr_multiplier=2.0,
        )
        assert result["trailing_stop_price"] == Decimal("95.0000")
        assert result["stop_type"] == "fixed_pct"

    def test_atr_multiplier_custom(self):
        # ATR = $2, multiplier = 3 => stop = $110 - $6 = $104
        result = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("110"),
            high_water_mark=Decimal("110"),
            atr=2.0,
            atr_multiplier=3.0,
        )
        assert result["trailing_stop_price"] == Decimal("104.0000")
        assert result["stop_type"] == "atr"

    def test_zero_atr_falls_back_to_fixed(self):
        result = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("100"),
            high_water_mark=Decimal("100"),
            atr=0.0,
        )
        assert result["stop_type"] == "fixed_pct"
        assert result["trailing_stop_price"] == Decimal("95.0000")


class TestHighWaterMarkRatchet:
    """Price goes up, stop follows; price comes back, stop stays."""

    def test_hwm_ratchets_up(self):
        # Start at $100
        r1 = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("100"),
            high_water_mark=None,
            atr=1.0,
        )
        assert r1["high_water_mark"] == Decimal("100")

        # Price rises to $110
        r2 = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("110"),
            high_water_mark=r1["high_water_mark"],
            atr=1.0,
            previous_stop=r1["trailing_stop_price"],
        )
        assert r2["high_water_mark"] == Decimal("110")
        # ATR stop = 110 - 2 = 108, fixed stop = 95 => uses ATR
        assert r2["trailing_stop_price"] == Decimal("108.0000")

        # Price falls back to $105 — HWM stays at $110, stop stays at $108
        r3 = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("105"),
            high_water_mark=r2["high_water_mark"],
            atr=1.0,
            previous_stop=r2["trailing_stop_price"],
        )
        assert r3["high_water_mark"] == Decimal("110")
        assert r3["trailing_stop_price"] == Decimal("108.0000")

    def test_hwm_continues_ratcheting(self):
        # Price at $120, HWM was $110
        result = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("120"),
            high_water_mark=Decimal("110"),
            atr=1.0,
        )
        assert result["high_water_mark"] == Decimal("120")
        # ATR stop = 120 - 2 = 118
        assert result["trailing_stop_price"] == Decimal("118.0000")


class TestStopNeverMovesDown:
    """Ratchet guarantee: new stop >= previous stop."""

    def test_stop_never_decreases(self):
        # First: price at $115, ATR = $1 => stop at $113
        r1 = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("115"),
            high_water_mark=Decimal("115"),
            atr=1.0,
        )
        assert r1["trailing_stop_price"] == Decimal("113.0000")

        # ATR widens to $5 => would-be stop = $115 - $10 = $105
        # But previous stop was $113, so it stays at $113
        r2 = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("112"),
            high_water_mark=r1["high_water_mark"],
            atr=5.0,
            previous_stop=r1["trailing_stop_price"],
        )
        assert r2["trailing_stop_price"] == Decimal("113.0000")

    def test_stop_never_below_fixed_floor(self):
        # Even without previous_stop, the fixed floor protects
        result = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("90"),
            high_water_mark=Decimal("100"),
            atr=None,
        )
        # Fixed 5% of entry = $95; stop is at $95 even though price is $90
        assert result["trailing_stop_price"] == Decimal("95.0000")

    def test_previous_stop_respected_even_with_no_atr(self):
        # Previous stop was $108, no ATR now, fixed floor = $95
        # Should keep $108
        result = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("105"),
            high_water_mark=Decimal("110"),
            atr=None,
            previous_stop=Decimal("108"),
        )
        assert result["trailing_stop_price"] == Decimal("108")


class TestDistancePct:
    """distance_pct represents how far above the stop the current price is."""

    def test_positive_distance(self):
        result = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("100"),
            high_water_mark=None,
            atr=None,
        )
        # Stop at $95, price at $100 => distance = (100 - 95) / 100 * 100 = 5%
        assert result["distance_pct"] == 5.0

    def test_negative_distance_when_breached(self):
        result = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("90"),
            high_water_mark=Decimal("100"),
            atr=None,
        )
        # Stop at $95, price at $90 => distance = (90 - 95) / 90 * 100 = -5.56
        assert result["distance_pct"] < 0


class TestUpdateTrailingStop:
    """update_trailing_stop — float-I/O wrapper around compute_trailing_stop."""

    def test_ratchets_hwm_up_on_new_high(self):
        state = {"high_water_mark": 100.0, "trailing_stop_price": 95.0}
        result = update_trailing_stop(state, current_price=105.0, atr=2.0, entry_price=90.0)
        assert result["high_water_mark"] == 105.0
        # ATR stop = 105 - 4 = 101; floor = 90*0.95 = 85.5 => uses ATR
        assert result["trailing_stop_price"] > 95.0
        assert result["trailing_stop_price"] >= 90.0 * 0.95

    def test_hwm_unchanged_on_pullback(self):
        state = {"high_water_mark": 105.0, "trailing_stop_price": 101.0}
        result = update_trailing_stop(state, current_price=103.0, atr=2.0, entry_price=90.0)
        assert result["high_water_mark"] == 105.0

    def test_stop_unchanged_on_pullback(self):
        # ATR stop for HWM=105 at atr=2 => 105-4=101; prev stop=101 => unchanged
        state = {"high_water_mark": 105.0, "trailing_stop_price": 101.0}
        result = update_trailing_stop(state, current_price=103.0, atr=2.0, entry_price=90.0)
        assert result["trailing_stop_price"] == 101.0

    def test_stop_monotonic_never_decreases(self):
        # Simulate price rising then falling while ATR widens
        state1 = {"high_water_mark": 115.0, "trailing_stop_price": 113.0}
        # ATR widens from 1 to 5 => would-be stop = 115 - 10 = 105, but prev=113
        result = update_trailing_stop(state1, current_price=112.0, atr=5.0, entry_price=100.0)
        assert result["trailing_stop_price"] >= 113.0

    def test_breach_detected_via_intraday_check(self):
        """Breach: current_price <= trailing_stop_price fires the trigger."""
        from decimal import Decimal
        from scorched.intraday import check_trailing_stop_breach
        # price at 108, stop at 110 => breached
        result = check_trailing_stop_breach(Decimal("108"), Decimal("110"))
        assert result.passed is False
        assert "108" in result.reason

    def test_no_breach_when_above_stop(self):
        from decimal import Decimal
        from scorched.intraday import check_trailing_stop_breach
        result = check_trailing_stop_breach(Decimal("112"), Decimal("110"))
        assert result.passed is True

    def test_fallback_when_atr_zero_no_crash(self, caplog):
        """ATR=0 falls back to fixed-pct stop — no exception."""
        state = {"high_water_mark": 100.0, "trailing_stop_price": 95.0}
        with caplog.at_level(logging.WARNING):
            result = update_trailing_stop(state, current_price=100.0, atr=0.0, entry_price=100.0)
        assert result["trailing_stop_price"] == 95.0  # fixed 5% floor

    def test_fallback_when_atr_none_no_crash(self):
        """ATR=None (passed as 0) falls back gracefully."""
        state = {"high_water_mark": 100.0, "trailing_stop_price": 95.0}
        result = update_trailing_stop(state, current_price=100.0, atr=0.0, entry_price=100.0)
        assert result["trailing_stop_price"] is not None
        assert result["high_water_mark"] is not None

    def test_returns_new_dict_does_not_mutate(self):
        state = {"high_water_mark": 100.0, "trailing_stop_price": 95.0}
        result = update_trailing_stop(state, current_price=110.0, atr=2.0, entry_price=100.0)
        assert state["high_water_mark"] == 100.0  # original unchanged
        assert result is not state
