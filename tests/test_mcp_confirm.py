"""MCP confirm_trade must enforce the same audit C1 contract as REST.

Tests validate that:
1. Client-supplied execution_price/shares are IGNORED — stored rec quantity wins.
2. Gate failure prevents broker call.
Both tests go through validate_and_submit_trade (the shared helper).
"""
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scorched.models import RecommendationSession, TradeRecommendation
from scorched.services.trade_execution import validate_and_submit_trade


# ── fixtures ──────────────────────────────────────────────────────────────────

async def _make_rec(db_session, symbol="AAPL", action="buy", quantity=Decimal("10"),
                    suggested_price=Decimal("150.00")):
    """Create a minimal RecommendationSession + TradeRecommendation for testing."""
    session = RecommendationSession(session_date=date.today())
    db_session.add(session)
    await db_session.flush()
    rec = TradeRecommendation(
        session_id=session.id,
        symbol=symbol, action=action, quantity=quantity,
        suggested_price=suggested_price, confidence="high",
        reasoning="test", key_risks="", status="pending",
    )
    db_session.add(rec)
    await db_session.commit()
    return rec


# ── C1-MCP test 1: stored qty wins over any client qty ───────────────────────

@pytest.mark.asyncio
async def test_validate_and_submit_uses_stored_rec_quantity(db_session):
    """C1-MCP: validate_and_submit_trade must use stored rec quantity, not any client value.

    This verifies the shared helper (used by both REST and MCP) respects the
    server-decides contract. The MCP tool ignores client-supplied shares/price
    and delegates entirely to this helper.
    """
    rec = await _make_rec(db_session, quantity=Decimal("10"), suggested_price=Decimal("150.00"))

    fake_broker = AsyncMock()
    fake_broker.submit_buy = AsyncMock(return_value={
        "status": "submitted",
        "filled_qty": Decimal("10"),
        "filled_avg_price": Decimal("150.00"),
    })

    mock_gate = MagicMock()
    mock_gate.passed = True

    with patch("scorched.services.alpaca_data.fetch_snapshots_sync",
               return_value={"AAPL": {"current_price": 150.5}}), \
         patch("scorched.services.trade_execution.get_broker", return_value=fake_broker), \
         patch("scorched.services.recommender._compute_portfolio_total_value",
               return_value=Decimal("100000")), \
         patch("scorched.services.recommender._get_sector_for_symbol",
               return_value="Technology"), \
         patch("scorched.services.trade_execution.run_all_buy_gates", return_value=mock_gate), \
         patch("scorched.services.trade_execution.load_strategy_json", return_value={}):

        result = await validate_and_submit_trade(rec.id, db_session)

    # Broker must have been called with the STORED quantity (10), not any hypothetical
    # client-supplied value like 9999.
    fake_broker.submit_buy.assert_called_once()
    call_kwargs = fake_broker.submit_buy.call_args.kwargs
    assert call_kwargs["qty"] == Decimal("10"), (
        f"Expected stored qty=10, got {call_kwargs['qty']}"
    )
    assert result.filled_qty == Decimal("10")
    assert result.symbol == "AAPL"
    assert result.action == "buy"


# ── C1-MCP test 2: gate failure prevents broker call ─────────────────────────

@pytest.mark.asyncio
async def test_validate_and_submit_gate_failure_prevents_broker_call(db_session):
    """C1-MCP: gate failure must prevent broker call — both REST and MCP use this helper."""
    rec = await _make_rec(db_session, quantity=Decimal("10"), suggested_price=Decimal("150.00"))

    fake_broker = AsyncMock()
    fake_broker.submit_buy = AsyncMock()

    mock_gate = MagicMock()
    mock_gate.passed = False
    mock_gate.reason = "cash floor would breach"

    with patch("scorched.services.alpaca_data.fetch_snapshots_sync",
               return_value={"AAPL": {"current_price": 150.5}}), \
         patch("scorched.services.trade_execution.get_broker", return_value=fake_broker), \
         patch("scorched.services.recommender._compute_portfolio_total_value",
               return_value=Decimal("100000")), \
         patch("scorched.services.recommender._get_sector_for_symbol",
               return_value="Technology"), \
         patch("scorched.services.trade_execution.run_all_buy_gates", return_value=mock_gate), \
         patch("scorched.services.trade_execution.load_strategy_json", return_value={}):

        with pytest.raises(ValueError) as exc_info:
            await validate_and_submit_trade(rec.id, db_session)

    assert "gate" in str(exc_info.value).lower() or "cash floor" in str(exc_info.value).lower(), (
        f"Expected gate/cash-floor rejection, got: {exc_info.value}"
    )
    fake_broker.submit_buy.assert_not_called()
