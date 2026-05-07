"""Tests for AlpacaBroker — mocked Alpaca SDK calls."""
import pytest
import pytest_asyncio
from decimal import Decimal
from unittest.mock import MagicMock, patch

from scorched.broker.alpaca import AlpacaBroker


@pytest.fixture
def alpaca_broker(db_session, mock_alpaca_client):
    return AlpacaBroker(db_session, mock_alpaca_client)


def _make_order(status="filled", filled_qty="2", filled_avg_price="150.00", symbol="AAPL"):
    order = MagicMock()
    order.id = "order-abc-123"
    order.status.value = status
    order.filled_qty = filled_qty
    order.filled_avg_price = filled_avg_price
    order.symbol = symbol
    return order


@pytest.mark.asyncio
async def test_alpaca_submit_buy_success(alpaca_broker, mock_alpaca_client):
    mock_alpaca_client.submit_order.return_value = _make_order()
    mock_alpaca_client.get_order_by_id.return_value = _make_order()
    result = await alpaca_broker.submit_buy(
        symbol="AAPL",
        qty=Decimal("2"),
        limit_price=Decimal("150.00"),
        recommendation_id=None,
    )
    # AlpacaBroker is fire-and-forget: submit returns "submitted", fills recorded by Phase 2.5 reconciler
    assert result["status"] == "submitted"
    assert result["order_id"] == "order-abc-123"
    mock_alpaca_client.submit_order.assert_called_once()


@pytest.mark.asyncio
async def test_alpaca_submit_sell_success(alpaca_broker, mock_alpaca_client):
    # Mock Alpaca position so the sell guard passes (submit_sell calls get_open_position)
    mock_position = MagicMock()
    mock_position.qty = "1"
    mock_alpaca_client.get_open_position.return_value = mock_position

    sell_order = _make_order(symbol="NVDA", filled_qty="1", filled_avg_price="200.00")
    mock_alpaca_client.submit_order.return_value = sell_order
    mock_alpaca_client.get_order_by_id.return_value = sell_order
    result = await alpaca_broker.submit_sell(
        symbol="NVDA",
        qty=Decimal("1"),
        limit_price=Decimal("200.00"),
        recommendation_id=None,
    )
    # AlpacaBroker is fire-and-forget: sell also returns "submitted"
    assert result["status"] == "submitted"
    assert result["order_id"] == "order-abc-123"


@pytest.mark.asyncio
async def test_alpaca_get_positions(alpaca_broker, mock_alpaca_client):
    pos = MagicMock()
    pos.symbol = "AAPL"
    pos.qty = "5"
    pos.avg_entry_price = "148.50"
    pos.market_value = "750.00"
    pos.unrealized_pl = "7.50"
    mock_alpaca_client.get_all_positions.return_value = [pos]

    positions = await alpaca_broker.get_positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "AAPL"
    assert positions[0]["qty"] == Decimal("5")


@pytest.mark.asyncio
async def test_alpaca_get_account(alpaca_broker, mock_alpaca_client):
    account = await alpaca_broker.get_account()
    assert account["status"] == "ACTIVE"
    assert account["buying_power"] == "950.00"


@pytest.mark.asyncio
async def test_alpaca_get_order_status(alpaca_broker, mock_alpaca_client):
    mock_alpaca_client.get_order_by_id.return_value = _make_order(status="filled")
    status = await alpaca_broker.get_order_status("order-abc-123")
    assert status["status"] == "filled"


@pytest.mark.asyncio
async def test_alpaca_submit_sell_rejects_missing_alpaca_position_by_default(
    alpaca_broker, mock_alpaca_client, monkeypatch
):
    """Paper-mode Alpaca sells should not silently fall back to DB-only PaperBroker by default."""
    monkeypatch.setattr("scorched.config.settings.broker_mode", "alpaca_paper")
    monkeypatch.setattr("scorched.config.settings.allow_paper_fallback_sell", False, raising=False)
    mock_alpaca_client.get_open_position.side_effect = Exception("40410000 position not found")

    with pytest.raises(ValueError, match="no position on Alpaca"):
        await alpaca_broker.submit_sell(
            symbol="MSFT",
            qty=Decimal("1"),
            limit_price=Decimal("250.00"),
            recommendation_id=None,
        )

    mock_alpaca_client.submit_order.assert_not_called()


@pytest.mark.asyncio
async def test_alpaca_submit_sell_allows_legacy_paper_fallback_when_enabled(
    alpaca_broker, mock_alpaca_client, monkeypatch
):
    """Legacy DB-only paper fallback is still available behind an explicit flag."""
    monkeypatch.setattr("scorched.config.settings.broker_mode", "alpaca_paper")
    monkeypatch.setattr("scorched.config.settings.allow_paper_fallback_sell", True, raising=False)
    mock_alpaca_client.get_open_position.side_effect = Exception("40410000 position not found")

    fallback_result = {"status": "filled", "symbol": "MSFT", "filled_qty": Decimal("1")}
    with patch("scorched.broker.paper.PaperBroker.submit_sell", return_value=fallback_result) as mock_fallback, \
         patch("scorched.services.telegram.send_telegram", return_value=None):
        result = await alpaca_broker.submit_sell(
            symbol="MSFT",
            qty=Decimal("1"),
            limit_price=Decimal("250.00"),
            recommendation_id=None,
        )

    assert result == fallback_result
    mock_fallback.assert_awaited_once()
    mock_alpaca_client.submit_order.assert_not_called()
