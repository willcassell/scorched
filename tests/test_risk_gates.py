from decimal import Decimal

from scorched.risk_gates import check_cash_floor, CashFloorResult, check_holdings_cap


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


class TestHoldingsCap:
    def test_passes_when_under_cap_and_new_symbol(self):
        result = check_holdings_cap(
            held_symbols={"AAPL", "MSFT"},
            accepted_new_symbols=set(),
            proposed_symbol="NVDA",
            max_holdings=10,
        )
        assert result.passed is True

    def test_rejects_at_cap_with_new_symbol(self):
        held = {f"S{i}" for i in range(10)}
        result = check_holdings_cap(
            held_symbols=held,
            accepted_new_symbols=set(),
            proposed_symbol="NEW",
            max_holdings=10,
        )
        assert result.passed is False

    def test_add_to_existing_holding_passes_at_cap(self):
        held = {f"S{i}" for i in range(10)}
        result = check_holdings_cap(
            held_symbols=held,
            accepted_new_symbols=set(),
            proposed_symbol="S0",  # already held
            max_holdings=10,
        )
        assert result.passed is True

    def test_cumulative_new_buys_breach_cap(self):
        held = {f"S{i}" for i in range(8)}
        # Two prior new buys accepted -> 10 effective holdings.
        accepted = {"NEW1", "NEW2"}
        result = check_holdings_cap(
            held_symbols=held,
            accepted_new_symbols=accepted,
            proposed_symbol="NEW3",
            max_holdings=10,
        )
        assert result.passed is False


from scorched.risk_gates import check_position_cap


class TestPositionCap:
    def test_pure_new_buy_under_cap_passes(self):
        # $100k total, 33% cap = $33k. Buy $30k. PASS.
        result = check_position_cap(
            existing_market_value=Decimal("0"),
            buy_notional=Decimal("30000"),
            total_portfolio_value=Decimal("100000"),
            max_position_pct=Decimal("33"),
        )
        assert result.passed is True

    def test_pure_new_buy_over_cap_rejects(self):
        result = check_position_cap(
            existing_market_value=Decimal("0"),
            buy_notional=Decimal("35000"),
            total_portfolio_value=Decimal("100000"),
            max_position_pct=Decimal("33"),
        )
        assert result.passed is False

    def test_add_on_buy_post_trade_breaches_cap(self):
        # Existing $25k, buy $10k -> $35k post-trade vs $33k cap. FAIL.
        result = check_position_cap(
            existing_market_value=Decimal("25000"),
            buy_notional=Decimal("10000"),
            total_portfolio_value=Decimal("100000"),
            max_position_pct=Decimal("33"),
        )
        assert result.passed is False
        assert result.projected_pct > 33

    def test_add_on_buy_within_cap_passes(self):
        # Existing $20k, buy $10k -> $30k. PASS.
        result = check_position_cap(
            existing_market_value=Decimal("20000"),
            buy_notional=Decimal("10000"),
            total_portfolio_value=Decimal("100000"),
            max_position_pct=Decimal("33"),
        )
        assert result.passed is True
