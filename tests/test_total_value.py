"""Portfolio total_value must reflect live market prices, not cost basis."""
from decimal import Decimal
from types import SimpleNamespace


def test_portfolio_total_value_uses_live_prices():
    from scorched.services.recommender import _compute_portfolio_total_value

    positions = [
        SimpleNamespace(symbol="AAPL", shares=Decimal("10"), avg_cost_basis=Decimal("100")),
        SimpleNamespace(symbol="MSFT", shares=Decimal("5"), avg_cost_basis=Decimal("200")),
    ]
    cash = Decimal("5000")
    price_data = {
        "AAPL": {"current_price": Decimal("150")},  # +50%
        "MSFT": {"current_price": Decimal("180")},  # -10%
    }
    total = _compute_portfolio_total_value(cash, positions, price_data)
    # Expected: 5000 + 10*150 + 5*180 = 5000 + 1500 + 900 = 7400
    assert total == Decimal("7400")


def test_portfolio_total_value_falls_back_to_cost_basis_if_price_missing():
    from scorched.services.recommender import _compute_portfolio_total_value

    positions = [
        SimpleNamespace(symbol="WEIRD", shares=Decimal("10"), avg_cost_basis=Decimal("50")),
    ]
    cash = Decimal("1000")
    price_data = {}  # No live price
    total = _compute_portfolio_total_value(cash, positions, price_data)
    # Fall back: 1000 + 10*50 = 1500
    assert total == Decimal("1500")
