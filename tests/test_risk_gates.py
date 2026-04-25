from decimal import Decimal

from scorched.risk_gates import check_cash_floor, CashFloorResult


class TestCashFloor:
    def test_passes_when_buy_leaves_floor_intact(self):
        # $100k total, $50k cash, 10% floor = $10k min. Buy $30k -> $20k cash. PASS.
        result = check_cash_floor(
            current_cash=Decimal("50000"),
            total_portfolio_value=Decimal("100000"),
            buy_notional=Decimal("30000"),
            reserve_pct=Decimal("0.10"),
        )
        assert result.passed is True
        assert result.projected_cash == Decimal("20000")
        assert result.floor == Decimal("10000")

    def test_rejects_when_buy_breaches_floor(self):
        result = check_cash_floor(
            current_cash=Decimal("50000"),
            total_portfolio_value=Decimal("100000"),
            buy_notional=Decimal("45000"),
            reserve_pct=Decimal("0.10"),
        )
        assert result.passed is False
        assert result.projected_cash == Decimal("5000")
        assert result.floor == Decimal("10000")

    def test_floor_uses_total_value_not_cash(self):
        # Regression for audit H1: floor must be based on total, not current cash.
        # If formula were cash * 0.10, floor would be $1000 and buy would pass.
        # Correct formula: total * 0.10 = $10000, buy fails.
        result = check_cash_floor(
            current_cash=Decimal("10000"),
            total_portfolio_value=Decimal("100000"),
            buy_notional=Decimal("5000"),
            reserve_pct=Decimal("0.10"),
        )
        assert result.passed is False  # 10k - 5k = 5k < 10k floor
        assert result.floor == Decimal("10000")  # not Decimal("1000")

    def test_passes_at_exact_floor(self):
        result = check_cash_floor(
            current_cash=Decimal("50000"),
            total_portfolio_value=Decimal("100000"),
            buy_notional=Decimal("40000"),
            reserve_pct=Decimal("0.10"),
        )
        assert result.passed is True
        assert result.projected_cash == Decimal("10000")

    def test_zero_total_value_fails_closed(self):
        result = check_cash_floor(
            current_cash=Decimal("0"),
            total_portfolio_value=Decimal("0"),
            buy_notional=Decimal("100"),
            reserve_pct=Decimal("0.10"),
        )
        assert result.passed is False


def test_cumulative_two_buys_breach_floor():
    """Two buys that each fit individually but together breach the floor."""
    cash = Decimal("50000")
    total = Decimal("100000")
    pct = Decimal("0.10")  # floor = $10k
    # First buy $30k -> $20k cash. Passes.
    r1 = check_cash_floor(cash, total, Decimal("30000"), pct)
    assert r1.passed
    # Second buy $15k against running cash $20k -> $5k. Below floor.
    r2 = check_cash_floor(r1.projected_cash, total, Decimal("15000"), pct)
    assert not r2.passed
