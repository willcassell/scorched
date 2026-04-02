"""Tests for ATR-based trailing stop logic."""

from decimal import Decimal

from scorched.trailing_stops import compute_trailing_stop


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
