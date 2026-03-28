"""Tests for PaperBroker — wraps existing DB logic."""
import pytest
import pytest_asyncio
from decimal import Decimal
from datetime import date

from scorched.broker.paper import PaperBroker
from scorched.models import Position


@pytest.mark.asyncio
async def test_paper_buy_deducts_cash_and_creates_position(db_session):
    broker = PaperBroker(db_session)
    result = await broker.submit_buy(
        symbol="AAPL",
        qty=Decimal("2"),
        limit_price=Decimal("150.00"),
        recommendation_id=None,
    )
    assert result["status"] == "filled"
    assert result["filled_qty"] == Decimal("2")
    assert result["filled_avg_price"] == Decimal("150.00")
    assert result["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_paper_buy_insufficient_cash_raises(db_session):
    broker = PaperBroker(db_session)
    with pytest.raises(ValueError, match="Insufficient cash"):
        await broker.submit_buy(
            symbol="AAPL",
            qty=Decimal("100"),
            limit_price=Decimal("150.00"),
            recommendation_id=None,
        )


@pytest.mark.asyncio
async def test_paper_sell_no_position_raises(db_session):
    broker = PaperBroker(db_session)
    with pytest.raises(ValueError, match="No position found"):
        await broker.submit_sell(
            symbol="AAPL",
            qty=Decimal("1"),
            limit_price=Decimal("150.00"),
            recommendation_id=None,
        )


@pytest.mark.asyncio
async def test_paper_get_positions(db_session):
    broker = PaperBroker(db_session)
    # Buy first
    await broker.submit_buy(
        symbol="NVDA",
        qty=Decimal("1"),
        limit_price=Decimal("200.00"),
        recommendation_id=None,
    )
    positions = await broker.get_positions()
    assert len(positions) >= 1
    nvda = [p for p in positions if p["symbol"] == "NVDA"]
    assert len(nvda) == 1
    assert nvda[0]["qty"] == Decimal("1")


@pytest.mark.asyncio
async def test_paper_get_account(db_session):
    broker = PaperBroker(db_session)
    account = await broker.get_account()
    assert "cash" in account
    assert "buying_power" in account
    assert "equity" in account
