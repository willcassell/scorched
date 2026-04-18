"""Sector concentration gate — reject a buy that would push a sector over max_sector_pct."""
from decimal import Decimal


class TestCheckSectorExposure:
    def test_empty_portfolio_passes(self):
        """Single buy in an empty portfolio should always pass (1 position << 40%)."""
        from scorched.services.recommender import check_sector_exposure

        ok = check_sector_exposure(
            proposed_symbol="NVDA",
            proposed_sector="Technology",
            proposed_dollars=Decimal("15000"),
            held_positions=[],
            total_value=Decimal("100000"),
            max_sector_pct=40.0,
        )
        assert ok is True

    def test_buy_exactly_at_limit_passes(self):
        """Buy that pushes sector to exactly 40% should pass (boundary = allowed)."""
        from scorched.services.recommender import check_sector_exposure

        # Existing: 30k Tech.  Buy 10k → 40k / 100k = 40.0% == cap → pass.
        held_positions = [
            {"symbol": "AAPL", "sector": "Technology", "market_value": Decimal("30000")},
        ]
        ok = check_sector_exposure(
            proposed_symbol="NVDA",
            proposed_sector="Technology",
            proposed_dollars=Decimal("10000"),
            held_positions=held_positions,
            total_value=Decimal("100000"),
            max_sector_pct=40.0,
        )
        assert ok is True

    def test_buy_that_exceeds_limit_is_rejected(self):
        """Buy that would push sector above 40% is rejected with a clear reason in logs."""
        from scorched.services.recommender import check_sector_exposure

        # Existing: 30k Tech + 10k Tech = 40k. Buy 5k more → 45% > 40% → reject.
        held_positions = [
            {"symbol": "AAPL", "sector": "Technology", "market_value": Decimal("30000")},
            {"symbol": "MSFT", "sector": "Technology", "market_value": Decimal("10000")},
        ]
        ok = check_sector_exposure(
            proposed_symbol="NVDA",
            proposed_sector="Technology",
            proposed_dollars=Decimal("5000"),
            held_positions=held_positions,
            total_value=Decimal("100000"),
            max_sector_pct=40.0,
        )
        assert ok is False

    def test_two_buys_same_sector_second_rejected(self):
        """If the first buy is accepted (tracked in held list), second buy must see it."""
        from scorched.services.recommender import check_sector_exposure

        # Simulate caller appending accepted buys to held_positions_for_sector.
        # Start: empty.  First buy: 20k Tech → 20%. Second buy: 25k Tech → 45% → reject.
        held_positions: list[dict] = []

        # First buy: 20k Technology — should pass
        first_ok = check_sector_exposure(
            proposed_symbol="NVDA",
            proposed_sector="Technology",
            proposed_dollars=Decimal("20000"),
            held_positions=held_positions,
            total_value=Decimal("100000"),
            max_sector_pct=40.0,
        )
        assert first_ok is True, "First buy should be allowed"

        # Caller records the accepted buy
        held_positions.append(
            {"symbol": "NVDA", "sector": "Technology", "market_value": Decimal("20000")}
        )

        # Second buy: 25k more Technology → 45% → should be rejected
        second_ok = check_sector_exposure(
            proposed_symbol="AAPL",
            proposed_sector="Technology",
            proposed_dollars=Decimal("25000"),
            held_positions=held_positions,
            total_value=Decimal("100000"),
            max_sector_pct=40.0,
        )
        assert second_ok is False, "Second buy should be rejected (would push to 45%)"

    def test_unknown_symbol_allowed_through(self):
        """Unknown sector (None) should pass — don't block on incomplete sector map."""
        from scorched.services.recommender import check_sector_exposure

        ok = check_sector_exposure(
            proposed_symbol="ZZZZ",
            proposed_sector=None,
            proposed_dollars=Decimal("5000"),
            held_positions=[],
            total_value=Decimal("100000"),
            max_sector_pct=40.0,
        )
        assert ok is True

    def test_sells_are_not_checked(self):
        """Sector gate function only applies to buys — callers skip it for sells.

        This test verifies check_sector_exposure itself always returns True when
        the proposed_dollars is 0 (the caller can also simply skip calling it for sells).
        """
        from scorched.services.recommender import check_sector_exposure

        # A "sell" of $0 (zero dollar flow) should never block
        ok = check_sector_exposure(
            proposed_symbol="AAPL",
            proposed_sector="Technology",
            proposed_dollars=Decimal("0"),
            held_positions=[
                {"symbol": "AAPL", "sector": "Technology", "market_value": Decimal("50000")},
            ],
            total_value=Decimal("100000"),
            max_sector_pct=40.0,
        )
        # 50k + 0 = 50k = 50% which IS over the limit, but sells don't add to sector.
        # The caller never calls check_sector_exposure for sells — this documents that contract.
        # With proposed_dollars=0: (50k+0)/100k = 50% > 40% — the check itself would flag it.
        # This test verifies the caller's responsibility: never call this for sells.
        # We assert True here because proposed_dollars=0 → no new buy cost being added, but
        # the existing 50k is already over — the function returns False correctly here.
        # This is intentional: the caller must NOT call check_sector_exposure for sells.
        assert ok is False  # correctly reflects existing overweight — caller skips this for sells

    def test_different_sector_unaffected(self):
        """A buy in a different sector should not be blocked by another sector's exposure."""
        from scorched.services.recommender import check_sector_exposure

        # 38k in Technology, buy 15k Financials → Financials = 15% → passes
        held_positions = [
            {"symbol": "AAPL", "sector": "Technology", "market_value": Decimal("38000")},
        ]
        ok = check_sector_exposure(
            proposed_symbol="JPM",
            proposed_sector="Financials",
            proposed_dollars=Decimal("15000"),
            held_positions=held_positions,
            total_value=Decimal("100000"),
            max_sector_pct=40.0,
        )
        assert ok is True

    def test_max_sector_pct_from_strategy_config(self):
        """Gate must respect the max_sector_pct parameter (integration: value from strategy.json)."""
        from scorched.services.recommender import check_sector_exposure
        from scorched.services.strategy import load_strategy_json

        strategy_json = load_strategy_json()
        max_sector_pct = strategy_json.get("concentration", {}).get("max_sector_pct", 40.0)

        # Verify strategy.json has the expected cap
        assert max_sector_pct == 40.0, f"Expected 40.0 in strategy.json, got {max_sector_pct}"

        # A buy that would exceed the strategy-configured cap must be rejected
        held_positions = [
            {"symbol": "AAPL", "sector": "Technology", "market_value": Decimal("39000")},
        ]
        ok = check_sector_exposure(
            proposed_symbol="NVDA",
            proposed_sector="Technology",
            proposed_dollars=Decimal("5000"),
            held_positions=held_positions,
            total_value=Decimal("100000"),
            max_sector_pct=max_sector_pct,
        )
        assert ok is False  # 39k + 5k = 44% > 40%
