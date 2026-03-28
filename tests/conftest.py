"""Shared fixtures for tradebot tests."""
import asyncio
from decimal import Decimal
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from scorched.database import Base
from scorched.models import Portfolio, Position


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_session():
    """In-memory SQLite async session for testing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        # Seed portfolio
        portfolio = Portfolio(
            cash_balance=Decimal("1000.00"),
            starting_capital=Decimal("1000.00"),
        )
        session.add(portfolio)
        await session.commit()
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
def mock_alpaca_client():
    """Mock TradingClient for Alpaca tests."""
    client = MagicMock()
    # Mock account
    account = MagicMock()
    account.status = "ACTIVE"
    account.buying_power = "950.00"
    account.cash = "950.00"
    account.equity = "1000.00"
    account.trading_blocked = False
    client.get_account.return_value = account
    # Mock positions
    client.get_all_positions.return_value = []
    return client
