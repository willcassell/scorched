# Alpaca Broker Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate Alpaca as a broker backend with a paper/live toggle, add a pre-execution circuit breaker (Phase 1.5), and keep the existing paper-only DB flow as the default — so the system can paper-trade on Alpaca before ever touching real money.

**Architecture:** A `BrokerAdapter` ABC with two implementations: `PaperBroker` (current DB-only logic, unchanged) and `AlpacaBroker` (submits real orders via `alpaca-py`, polls for fills, syncs positions). A new `circuit_breaker` module gates buys at market open. Config in `.env` controls which broker is active. The recommendation engine, playbook, dashboard, and Phase 1/3 cron scripts are untouched.

**Tech Stack:** `alpaca-py` SDK, existing FastAPI + SQLAlchemy + asyncio stack, yfinance (for circuit breaker price checks)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/scorched/broker/__init__.py` | Package init, exports `get_broker()` factory |
| `src/scorched/broker/base.py` | `BrokerAdapter` ABC — `submit_buy`, `submit_sell`, `get_positions`, `get_account`, `get_order_status` |
| `src/scorched/broker/paper.py` | `PaperBroker` — wraps existing `apply_buy`/`apply_sell` unchanged |
| `src/scorched/broker/alpaca.py` | `AlpacaBroker` — submits orders via `alpaca-py`, polls fills, records to DB |
| `src/scorched/circuit_breaker.py` | Pre-execution gate checks (gap %, SPY drop, VIX spike, halts) |
| `src/scorched/config.py` | Add Alpaca + circuit breaker settings |
| `strategy.json` | Add `circuit_breaker` section with thresholds |
| `src/scorched/api/trades.py` | Route through `get_broker()` instead of direct `portfolio_svc` calls |
| `src/scorched/api/broker_status.py` | New endpoint: `GET /api/v1/broker/status` — account info, position reconciliation |
| `cron/tradebot_phase1_5.py` | New cron script: circuit breaker gate at 9:30 AM ET |
| `cron/tradebot_phase2.py` | Modify to use broker-aware confirm (limit orders, Alpaca fill tracking) |
| `tests/test_broker_base.py` | Tests for BrokerAdapter interface contract |
| `tests/test_paper_broker.py` | Tests for PaperBroker (ensures existing behavior preserved) |
| `tests/test_alpaca_broker.py` | Tests for AlpacaBroker (mocked SDK calls) |
| `tests/test_circuit_breaker.py` | Tests for all circuit breaker rules |
| `tests/conftest.py` | Shared fixtures: async DB session, mock Alpaca client |

---

## Task 1: Project Setup — Add Dependencies and Test Infrastructure

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Add alpaca-py and pytest dependencies to pyproject.toml**

```toml
# Add to [project] dependencies list:
    "alpaca-py>=0.30.0",

# Add new section:
[project.optional-dependencies]
test = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "aiosqlite>=0.20.0",
]
```

In `pyproject.toml`, add `"alpaca-py>=0.30.0",` to the `dependencies` list (after the `yfinance` line). Then add the `[project.optional-dependencies]` section after `[project]`.

- [ ] **Step 2: Create test package and conftest**

Create `tests/__init__.py` (empty file).

Create `tests/conftest.py`:

```python
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
```

- [ ] **Step 3: Install dependencies and verify pytest runs**

Run: `cd /home/ubuntu/tradebot && pip install -e ".[test]"`
Expected: Installation succeeds, alpaca-py and pytest installed.

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/ -v --co 2>&1 | head -20`
Expected: "no tests ran" or "collected 0 items" (no errors).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml tests/__init__.py tests/conftest.py
git commit -m "feat: add alpaca-py dependency and test infrastructure"
```

---

## Task 2: BrokerAdapter ABC and PaperBroker

**Files:**
- Create: `src/scorched/broker/__init__.py`
- Create: `src/scorched/broker/base.py`
- Create: `src/scorched/broker/paper.py`
- Create: `tests/test_broker_base.py`
- Create: `tests/test_paper_broker.py`

- [ ] **Step 1: Write failing tests for BrokerAdapter interface and PaperBroker**

Create `tests/test_broker_base.py`:

```python
"""Tests for BrokerAdapter ABC — verifies interface contract."""
import pytest
from scorched.broker.base import BrokerAdapter


def test_broker_adapter_cannot_be_instantiated():
    with pytest.raises(TypeError):
        BrokerAdapter()


def test_broker_adapter_defines_required_methods():
    required = {"submit_buy", "submit_sell", "get_positions", "get_account", "get_order_status"}
    abstract_methods = BrokerAdapter.__abstractmethods__
    assert required.issubset(abstract_methods)
```

Create `tests/test_paper_broker.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/test_broker_base.py tests/test_paper_broker.py -v 2>&1 | tail -20`
Expected: FAIL — `ModuleNotFoundError: No module named 'scorched.broker'`

- [ ] **Step 3: Implement BrokerAdapter ABC**

Create `src/scorched/broker/__init__.py`:

```python
"""Broker abstraction layer — paper or live trading."""
from .base import BrokerAdapter
from .paper import PaperBroker

__all__ = ["BrokerAdapter", "PaperBroker", "get_broker"]


def get_broker(db_session, alpaca_client=None):
    """Factory: returns AlpacaBroker if alpaca_client is provided, else PaperBroker."""
    if alpaca_client is not None:
        from .alpaca import AlpacaBroker
        return AlpacaBroker(db_session, alpaca_client)
    return PaperBroker(db_session)
```

Create `src/scorched/broker/base.py`:

```python
"""Abstract base class for broker adapters."""
from abc import ABC, abstractmethod
from decimal import Decimal


class BrokerAdapter(ABC):
    """Interface that all broker implementations must satisfy.

    Returns dicts rather than broker-specific objects so callers
    never depend on a concrete SDK.
    """

    @abstractmethod
    async def submit_buy(
        self,
        symbol: str,
        qty: Decimal,
        limit_price: Decimal,
        recommendation_id: int | None,
    ) -> dict:
        """Submit a buy order. Returns fill info dict with keys:
        status, filled_qty, filled_avg_price, symbol, order_id.
        """
        ...

    @abstractmethod
    async def submit_sell(
        self,
        symbol: str,
        qty: Decimal,
        limit_price: Decimal,
        recommendation_id: int | None,
    ) -> dict:
        """Submit a sell order. Returns fill info dict with same keys as submit_buy,
        plus realized_gain, tax_category.
        """
        ...

    @abstractmethod
    async def get_positions(self) -> list[dict]:
        """Return all open positions as list of dicts with keys:
        symbol, qty, avg_cost_basis, market_value, unrealized_pl.
        """
        ...

    @abstractmethod
    async def get_account(self) -> dict:
        """Return account summary with keys:
        cash, buying_power, equity, status.
        """
        ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> dict:
        """Return order status dict with keys:
        order_id, status, filled_qty, filled_avg_price.
        """
        ...
```

- [ ] **Step 4: Implement PaperBroker**

Create `src/scorched/broker/paper.py`:

```python
"""Paper broker — wraps existing DB portfolio logic."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Portfolio, Position
from ..services.portfolio import apply_buy, apply_sell


class PaperBroker:
    """Executes trades by writing directly to the database (no real broker).

    Fills are instant at the provided limit_price — identical to the
    existing paper-trading behavior.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def submit_buy(
        self,
        symbol: str,
        qty: Decimal,
        limit_price: Decimal,
        recommendation_id: int | None,
    ) -> dict:
        result = await apply_buy(
            self.db,
            recommendation_id=recommendation_id,
            symbol=symbol,
            shares=qty,
            execution_price=limit_price,
            executed_at=datetime.utcnow(),
        )
        return {
            "status": "filled",
            "filled_qty": qty,
            "filled_avg_price": limit_price,
            "symbol": symbol,
            "order_id": f"paper-{result.trade_id}",
            "trade_id": result.trade_id,
            "new_cash_balance": result.new_cash_balance,
        }

    async def submit_sell(
        self,
        symbol: str,
        qty: Decimal,
        limit_price: Decimal,
        recommendation_id: int | None,
    ) -> dict:
        result = await apply_sell(
            self.db,
            recommendation_id=recommendation_id,
            symbol=symbol,
            shares=qty,
            execution_price=limit_price,
            executed_at=datetime.utcnow(),
        )
        return {
            "status": "filled",
            "filled_qty": qty,
            "filled_avg_price": limit_price,
            "symbol": symbol,
            "order_id": f"paper-{result.trade_id}",
            "trade_id": result.trade_id,
            "new_cash_balance": result.new_cash_balance,
            "realized_gain": result.realized_gain,
            "tax_category": result.tax_category,
        }

    async def get_positions(self) -> list[dict]:
        rows = (await self.db.execute(select(Position))).scalars().all()
        return [
            {
                "symbol": p.symbol,
                "qty": p.shares,
                "avg_cost_basis": p.avg_cost_basis,
                "market_value": None,  # paper broker doesn't fetch live prices here
                "unrealized_pl": None,
            }
            for p in rows
        ]

    async def get_account(self) -> dict:
        portfolio = (await self.db.execute(select(Portfolio))).scalars().first()
        return {
            "cash": portfolio.cash_balance,
            "buying_power": portfolio.cash_balance,
            "equity": portfolio.cash_balance,  # approximate for paper
            "status": "ACTIVE",
        }

    async def get_order_status(self, order_id: str) -> dict:
        # Paper orders fill instantly — always "filled"
        return {
            "order_id": order_id,
            "status": "filled",
            "filled_qty": None,
            "filled_avg_price": None,
        }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/test_broker_base.py tests/test_paper_broker.py -v 2>&1 | tail -20`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/scorched/broker/ tests/test_broker_base.py tests/test_paper_broker.py
git commit -m "feat: add BrokerAdapter ABC and PaperBroker implementation"
```

---

## Task 3: AlpacaBroker Implementation

**Files:**
- Create: `src/scorched/broker/alpaca.py`
- Create: `tests/test_alpaca_broker.py`
- Modify: `src/scorched/config.py`

- [ ] **Step 1: Add Alpaca config settings**

In `src/scorched/config.py`, add these fields to the `Settings` class (after the `fred_api_key` line):

```python
    # Broker config
    broker_mode: str = "paper"  # "paper" = DB-only, "alpaca_paper" = Alpaca paper, "alpaca_live" = Alpaca live
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
```

- [ ] **Step 2: Write failing tests for AlpacaBroker**

Create `tests/test_alpaca_broker.py`:

```python
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
    result = await alpaca_broker.submit_buy(
        symbol="AAPL",
        qty=Decimal("2"),
        limit_price=Decimal("150.00"),
        recommendation_id=None,
    )
    assert result["status"] == "filled"
    assert result["order_id"] == "order-abc-123"
    mock_alpaca_client.submit_order.assert_called_once()


@pytest.mark.asyncio
async def test_alpaca_submit_sell_success(alpaca_broker, mock_alpaca_client):
    mock_alpaca_client.submit_order.return_value = _make_order(symbol="NVDA")
    result = await alpaca_broker.submit_sell(
        symbol="NVDA",
        qty=Decimal("1"),
        limit_price=Decimal("200.00"),
        recommendation_id=None,
    )
    assert result["status"] == "filled"
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/test_alpaca_broker.py -v 2>&1 | tail -20`
Expected: FAIL — `ModuleNotFoundError: No module named 'scorched.broker.alpaca'`

- [ ] **Step 4: Implement AlpacaBroker**

Create `src/scorched/broker/alpaca.py`:

```python
"""Alpaca broker — submits real orders via alpaca-py SDK."""
import asyncio
import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from ..models import Portfolio, Position, TradeHistory, TradeRecommendation
from ..tax import classify_gain, estimate_tax

logger = logging.getLogger(__name__)


class AlpacaBroker:
    """Submits orders to Alpaca and records fills in the local DB.

    Uses limit orders by default (limit_price from caller). Falls back to
    market orders only if limit_price is None.

    The local DB is updated after the order fills so that the dashboard,
    portfolio endpoint, and tax logic all stay consistent.
    """

    def __init__(self, db: AsyncSession, client: TradingClient):
        self.db = db
        self.client = client

    def _submit_order_sync(self, order_data):
        """Alpaca SDK is sync — call from executor."""
        return self.client.submit_order(order_data=order_data)

    def _get_order_sync(self, order_id: str):
        return self.client.get_order_by_id(order_id=order_id)

    async def submit_buy(
        self,
        symbol: str,
        qty: Decimal,
        limit_price: Decimal,
        recommendation_id: int | None,
    ) -> dict:
        order_data = LimitOrderRequest(
            symbol=symbol,
            qty=float(qty),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=float(limit_price),
        )

        loop = asyncio.get_event_loop()
        order = await loop.run_in_executor(None, self._submit_order_sync, order_data)

        # Wait for fill (poll up to 60s for day orders near open)
        filled_order = await self._wait_for_fill(str(order.id), timeout=60)

        status = filled_order.status.value if hasattr(filled_order.status, 'value') else str(filled_order.status)
        filled_qty = Decimal(str(filled_order.filled_qty)) if filled_order.filled_qty else Decimal("0")
        filled_price = Decimal(str(filled_order.filled_avg_price)) if filled_order.filled_avg_price else limit_price

        # Record in local DB if filled
        trade_id = None
        new_cash = None
        if status == "filled" and filled_qty > 0:
            trade_id, new_cash = await self._record_buy(
                symbol, filled_qty, filled_price, recommendation_id
            )

        return {
            "status": status,
            "filled_qty": filled_qty,
            "filled_avg_price": filled_price,
            "symbol": symbol,
            "order_id": str(order.id),
            "trade_id": trade_id,
            "new_cash_balance": new_cash,
        }

    async def submit_sell(
        self,
        symbol: str,
        qty: Decimal,
        limit_price: Decimal,
        recommendation_id: int | None,
    ) -> dict:
        order_data = LimitOrderRequest(
            symbol=symbol,
            qty=float(qty),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=float(limit_price),
        )

        loop = asyncio.get_event_loop()
        order = await loop.run_in_executor(None, self._submit_order_sync, order_data)

        filled_order = await self._wait_for_fill(str(order.id), timeout=60)

        status = filled_order.status.value if hasattr(filled_order.status, 'value') else str(filled_order.status)
        filled_qty = Decimal(str(filled_order.filled_qty)) if filled_order.filled_qty else Decimal("0")
        filled_price = Decimal(str(filled_order.filled_avg_price)) if filled_order.filled_avg_price else limit_price

        realized_gain = None
        tax_category = None
        trade_id = None
        new_cash = None
        if status == "filled" and filled_qty > 0:
            trade_id, new_cash, realized_gain, tax_category = await self._record_sell(
                symbol, filled_qty, filled_price, recommendation_id
            )

        return {
            "status": status,
            "filled_qty": filled_qty,
            "filled_avg_price": filled_price,
            "symbol": symbol,
            "order_id": str(order.id),
            "trade_id": trade_id,
            "new_cash_balance": new_cash,
            "realized_gain": realized_gain,
            "tax_category": tax_category,
        }

    async def get_positions(self) -> list[dict]:
        loop = asyncio.get_event_loop()
        positions = await loop.run_in_executor(None, self.client.get_all_positions)
        return [
            {
                "symbol": p.symbol,
                "qty": Decimal(str(p.qty)),
                "avg_cost_basis": Decimal(str(p.avg_entry_price)),
                "market_value": Decimal(str(p.market_value)),
                "unrealized_pl": Decimal(str(p.unrealized_pl)),
            }
            for p in positions
        ]

    async def get_account(self) -> dict:
        loop = asyncio.get_event_loop()
        account = await loop.run_in_executor(None, self.client.get_account)
        return {
            "cash": account.cash,
            "buying_power": account.buying_power,
            "equity": account.equity,
            "status": account.status,
        }

    async def get_order_status(self, order_id: str) -> dict:
        loop = asyncio.get_event_loop()
        order = await loop.run_in_executor(None, self._get_order_sync, order_id)
        status = order.status.value if hasattr(order.status, 'value') else str(order.status)
        return {
            "order_id": str(order.id),
            "status": status,
            "filled_qty": order.filled_qty,
            "filled_avg_price": order.filled_avg_price,
        }

    async def _wait_for_fill(self, order_id: str, timeout: int = 60):
        """Poll order status until terminal state or timeout."""
        terminal = {"filled", "canceled", "expired", "rejected"}
        loop = asyncio.get_event_loop()
        elapsed = 0
        interval = 2
        while elapsed < timeout:
            order = await loop.run_in_executor(None, self._get_order_sync, order_id)
            status = order.status.value if hasattr(order.status, 'value') else str(order.status)
            if status in terminal:
                return order
            await asyncio.sleep(interval)
            elapsed += interval
        logger.warning("Order %s did not reach terminal state in %ds", order_id, timeout)
        return order

    async def _record_buy(self, symbol, qty, price, recommendation_id):
        """Mirror the buy into local DB for dashboard/tax consistency."""
        portfolio = (await self.db.execute(select(Portfolio))).scalars().first()
        total_cost = (qty * price).quantize(Decimal("0.01"))

        # Update or create position
        pos = (await self.db.execute(
            select(Position).where(Position.symbol == symbol)
        )).scalars().first()

        if pos is None:
            pos = Position(
                symbol=symbol,
                shares=qty,
                avg_cost_basis=price,
                first_purchase_date=datetime.utcnow().date(),
            )
            self.db.add(pos)
        else:
            total_existing = pos.shares * pos.avg_cost_basis
            new_total = total_existing + (qty * price)
            pos.shares = pos.shares + qty
            pos.avg_cost_basis = (new_total / pos.shares).quantize(Decimal("0.0001"))

        portfolio.cash_balance = (portfolio.cash_balance - total_cost).quantize(Decimal("0.01"))

        if recommendation_id is not None:
            rec = (await self.db.execute(
                select(TradeRecommendation).where(TradeRecommendation.id == recommendation_id)
            )).scalars().first()
            if rec:
                rec.status = "confirmed"

        history = TradeHistory(
            recommendation_id=recommendation_id,
            symbol=symbol,
            action="buy",
            shares=qty,
            execution_price=price,
            total_value=total_cost,
            executed_at=datetime.utcnow(),
        )
        self.db.add(history)
        await self.db.commit()
        return history.id, portfolio.cash_balance

    async def _record_sell(self, symbol, qty, price, recommendation_id):
        """Mirror the sell into local DB."""
        portfolio = (await self.db.execute(select(Portfolio))).scalars().first()
        pos = (await self.db.execute(
            select(Position).where(Position.symbol == symbol)
        )).scalars().first()

        total_proceeds = (qty * price).quantize(Decimal("0.01"))
        realized_gain = ((price - pos.avg_cost_basis) * qty).quantize(Decimal("0.01")) if pos else Decimal("0")
        tax_cat = classify_gain(pos.first_purchase_date, datetime.utcnow().date()) if pos else "short_term"

        if pos:
            pos.shares -= qty
            if pos.shares <= Decimal("0"):
                await self.db.delete(pos)

        portfolio.cash_balance = (portfolio.cash_balance + total_proceeds).quantize(Decimal("0.01"))

        if recommendation_id is not None:
            rec = (await self.db.execute(
                select(TradeRecommendation).where(TradeRecommendation.id == recommendation_id)
            )).scalars().first()
            if rec:
                rec.status = "confirmed"

        history = TradeHistory(
            recommendation_id=recommendation_id,
            symbol=symbol,
            action="sell",
            shares=qty,
            execution_price=price,
            total_value=total_proceeds,
            executed_at=datetime.utcnow(),
            realized_gain=realized_gain,
            tax_category=tax_cat,
        )
        self.db.add(history)
        await self.db.commit()
        return history.id, portfolio.cash_balance, realized_gain, tax_cat
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/test_alpaca_broker.py -v 2>&1 | tail -20`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/scorched/broker/alpaca.py src/scorched/config.py tests/test_alpaca_broker.py
git commit -m "feat: add AlpacaBroker with order submission, fill polling, and DB mirroring"
```

---

## Task 4: Circuit Breaker Module

**Files:**
- Create: `src/scorched/circuit_breaker.py`
- Create: `tests/test_circuit_breaker.py`
- Modify: `strategy.json`

- [ ] **Step 1: Add circuit breaker thresholds to strategy.json**

Add a `"circuit_breaker"` key to `strategy.json`:

```json
{
  "objective": "growth",
  "rec_style": "adaptive",
  "no_trade_threshold": "adaptive",
  "rec_explanation": "brief",
  "hold_period": "3-10d",
  "entry_style": [],
  "sell_discipline": "full_target",
  "loss_management": "hard_stop",
  "partial_sell": "never",
  "sizing_style": "equal_weight",
  "concentration": {
    "max_position_pct": 20,
    "max_sector_pct": 40,
    "max_holdings": 5
  },
  "add_vs_rotate": "add_winners",
  "risk_guardrails": [],
  "market_regime": "always_active",
  "event_risk": [],
  "sectors": [],
  "notes": "",
  "circuit_breaker": {
    "enabled": true,
    "stock_gap_down_pct": 2.0,
    "stock_price_drift_pct": 1.5,
    "spy_gap_down_pct": 1.0,
    "vix_absolute_max": 30,
    "vix_spike_pct": 20.0
  }
}
```

- [ ] **Step 2: Write failing tests for circuit breaker**

Create `tests/test_circuit_breaker.py`:

```python
"""Tests for circuit breaker gate checks."""
import pytest
from decimal import Decimal
from unittest.mock import patch, AsyncMock

from scorched.circuit_breaker import (
    check_stock_gate,
    check_market_gate,
    run_circuit_breaker,
)


CB_CONFIG = {
    "enabled": True,
    "stock_gap_down_pct": 2.0,
    "stock_price_drift_pct": 1.5,
    "spy_gap_down_pct": 1.0,
    "vix_absolute_max": 30,
    "vix_spike_pct": 20.0,
}


class TestStockGate:
    def test_passes_when_price_stable(self):
        result = check_stock_gate(
            symbol="AAPL",
            suggested_price=Decimal("150.00"),
            current_price=Decimal("149.50"),
            prior_close=Decimal("150.00"),
            config=CB_CONFIG,
        )
        assert result.passed is True

    def test_fails_on_gap_down_from_close(self):
        # 3% gap down > 2% threshold
        result = check_stock_gate(
            symbol="AAPL",
            suggested_price=Decimal("150.00"),
            current_price=Decimal("145.00"),
            prior_close=Decimal("150.00"),
            config=CB_CONFIG,
        )
        assert result.passed is False
        assert "gap_down" in result.reason

    def test_fails_on_drift_from_suggested(self):
        # 2% drift > 1.5% threshold
        result = check_stock_gate(
            symbol="AAPL",
            suggested_price=Decimal("150.00"),
            current_price=Decimal("147.00"),
            prior_close=Decimal("149.00"),
            config=CB_CONFIG,
        )
        assert result.passed is False
        assert "drift" in result.reason


class TestMarketGate:
    def test_passes_when_market_calm(self):
        result = check_market_gate(
            spy_current=Decimal("500.00"),
            spy_prior_close=Decimal("501.00"),
            vix_current=Decimal("18.00"),
            vix_prior_close=Decimal("17.00"),
            config=CB_CONFIG,
        )
        assert result.passed is True

    def test_fails_on_spy_gap_down(self):
        # SPY down 1.5% > 1% threshold
        result = check_market_gate(
            spy_current=Decimal("492.50"),
            spy_prior_close=Decimal("500.00"),
            vix_current=Decimal("18.00"),
            vix_prior_close=Decimal("17.00"),
            config=CB_CONFIG,
        )
        assert result.passed is False
        assert "SPY" in result.reason

    def test_fails_on_vix_absolute(self):
        result = check_market_gate(
            spy_current=Decimal("499.00"),
            spy_prior_close=Decimal("500.00"),
            vix_current=Decimal("32.00"),
            vix_prior_close=Decimal("28.00"),
            config=CB_CONFIG,
        )
        assert result.passed is False
        assert "VIX" in result.reason

    def test_fails_on_vix_spike(self):
        # VIX jumped 25% > 20% threshold
        result = check_market_gate(
            spy_current=Decimal("499.00"),
            spy_prior_close=Decimal("500.00"),
            vix_current=Decimal("25.00"),
            vix_prior_close=Decimal("20.00"),
            config=CB_CONFIG,
        )
        assert result.passed is False
        assert "VIX" in result.reason

    def test_disabled_always_passes(self):
        disabled = {**CB_CONFIG, "enabled": False}
        result = check_market_gate(
            spy_current=Decimal("400.00"),
            spy_prior_close=Decimal("500.00"),
            vix_current=Decimal("50.00"),
            vix_prior_close=Decimal("20.00"),
            config=disabled,
        )
        assert result.passed is True
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/test_circuit_breaker.py -v 2>&1 | tail -20`
Expected: FAIL — `ModuleNotFoundError: No module named 'scorched.circuit_breaker'`

- [ ] **Step 4: Implement circuit breaker module**

Create `src/scorched/circuit_breaker.py`:

```python
"""Pre-execution circuit breaker — gates buy orders at market open.

Checks are pure functions (no I/O) so they're easy to test.
The `run_circuit_breaker` async function fetches live data and
calls the pure checkers.
"""
import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    passed: bool
    reason: str = ""


def check_stock_gate(
    symbol: str,
    suggested_price: Decimal,
    current_price: Decimal,
    prior_close: Decimal,
    config: dict,
) -> GateResult:
    """Check whether a single stock's price action disqualifies a buy."""
    if not config.get("enabled", True):
        return GateResult(passed=True)

    # Gap down from prior close
    if prior_close > 0:
        gap_pct = float((prior_close - current_price) / prior_close * 100)
        threshold = config.get("stock_gap_down_pct", 2.0)
        if gap_pct > threshold:
            return GateResult(
                passed=False,
                reason=f"{symbol} gap_down {gap_pct:.1f}% from prior close (threshold: {threshold}%)",
            )

    # Drift from Claude's suggested price
    if suggested_price > 0:
        drift_pct = float((suggested_price - current_price) / suggested_price * 100)
        threshold = config.get("stock_price_drift_pct", 1.5)
        if drift_pct > threshold:
            return GateResult(
                passed=False,
                reason=f"{symbol} drift {drift_pct:.1f}% below suggested ${suggested_price} (threshold: {threshold}%)",
            )

    return GateResult(passed=True)


def check_market_gate(
    spy_current: Decimal,
    spy_prior_close: Decimal,
    vix_current: Decimal,
    vix_prior_close: Decimal,
    config: dict,
) -> GateResult:
    """Check whether broad market conditions disqualify ALL buys."""
    if not config.get("enabled", True):
        return GateResult(passed=True)

    # SPY gap down
    if spy_prior_close > 0:
        spy_gap_pct = float((spy_prior_close - spy_current) / spy_prior_close * 100)
        threshold = config.get("spy_gap_down_pct", 1.0)
        if spy_gap_pct > threshold:
            return GateResult(
                passed=False,
                reason=f"SPY gap_down {spy_gap_pct:.1f}% (threshold: {threshold}%)",
            )

    # VIX absolute level
    vix_max = config.get("vix_absolute_max", 30)
    if float(vix_current) > vix_max:
        return GateResult(
            passed=False,
            reason=f"VIX at {float(vix_current):.1f} exceeds max {vix_max}",
        )

    # VIX overnight spike
    if vix_prior_close > 0:
        vix_spike_pct = float((vix_current - vix_prior_close) / vix_prior_close * 100)
        threshold = config.get("vix_spike_pct", 20.0)
        if vix_spike_pct > threshold:
            return GateResult(
                passed=False,
                reason=f"VIX spiked {vix_spike_pct:.1f}% overnight (threshold: {threshold}%)",
            )

    return GateResult(passed=True)


async def fetch_gate_data(symbols: list[str]) -> dict:
    """Fetch live prices for circuit breaker checks via yfinance.

    Returns dict with keys: spy_current, spy_prior_close, vix_current,
    vix_prior_close, and per-symbol current_price and prior_close.
    """
    import yfinance as yf

    all_symbols = list(set(symbols + ["SPY", "^VIX"]))

    def _fetch():
        data = {}
        for sym in all_symbols:
            try:
                ticker = yf.Ticker(sym)
                hist = ticker.history(period="5d")
                if len(hist) >= 2:
                    data[sym] = {
                        "current": Decimal(str(hist["Close"].iloc[-1])),
                        "prior_close": Decimal(str(hist["Close"].iloc[-2])),
                    }
                elif len(hist) == 1:
                    price = Decimal(str(hist["Close"].iloc[-1]))
                    data[sym] = {"current": price, "prior_close": price}
            except Exception as e:
                logger.warning("Circuit breaker: failed to fetch %s: %s", sym, e)
        return data

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch)


async def run_circuit_breaker(
    recommendations: list[dict],
    config: dict,
) -> list[dict]:
    """Run all gate checks against pending buy recommendations.

    Returns the input list with a `gate_result` key added to each dict.
    Sell recommendations are always passed through.
    """
    if not config.get("enabled", True):
        for rec in recommendations:
            rec["gate_result"] = GateResult(passed=True)
        return recommendations

    buy_symbols = [r["symbol"] for r in recommendations if r["action"] == "buy"]

    if not buy_symbols:
        for rec in recommendations:
            rec["gate_result"] = GateResult(passed=True)
        return recommendations

    data = await fetch_gate_data(buy_symbols)

    # Market-level gate
    spy_data = data.get("SPY", {})
    vix_data = data.get("^VIX", {})
    market_gate = check_market_gate(
        spy_current=spy_data.get("current", Decimal("0")),
        spy_prior_close=spy_data.get("prior_close", Decimal("0")),
        vix_current=vix_data.get("current", Decimal("0")),
        vix_prior_close=vix_data.get("prior_close", Decimal("0")),
        config=config,
    )

    for rec in recommendations:
        if rec["action"] == "sell":
            rec["gate_result"] = GateResult(passed=True)
            continue

        if not market_gate.passed:
            rec["gate_result"] = market_gate
            continue

        sym_data = data.get(rec["symbol"], {})
        rec["gate_result"] = check_stock_gate(
            symbol=rec["symbol"],
            suggested_price=Decimal(str(rec.get("suggested_price", 0))),
            current_price=sym_data.get("current", Decimal("0")),
            prior_close=sym_data.get("prior_close", Decimal("0")),
            config=config,
        )

    return recommendations
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/test_circuit_breaker.py -v 2>&1 | tail -20`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/scorched/circuit_breaker.py tests/test_circuit_breaker.py strategy.json
git commit -m "feat: add circuit breaker module with stock and market gate checks"
```

---

## Task 5: Wire Broker into Trade Confirmation Endpoint

**Files:**
- Modify: `src/scorched/api/trades.py`
- Modify: `src/scorched/broker/__init__.py`
- Modify: `src/scorched/main.py`

- [ ] **Step 1: Update broker factory to read config**

Replace the `get_broker` function in `src/scorched/broker/__init__.py`:

```python
"""Broker abstraction layer — paper or live trading."""
from .base import BrokerAdapter
from .paper import PaperBroker

__all__ = ["BrokerAdapter", "PaperBroker", "get_broker"]


def get_broker(db_session):
    """Factory: returns the broker configured in settings.broker_mode."""
    from ..config import settings

    if settings.broker_mode in ("alpaca_paper", "alpaca_live"):
        from alpaca.trading.client import TradingClient
        from .alpaca import AlpacaBroker

        is_paper = settings.broker_mode == "alpaca_paper"
        client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            paper=is_paper,
        )
        return AlpacaBroker(db_session, client)

    return PaperBroker(db_session)
```

- [ ] **Step 2: Update trades.py to use broker adapter**

Replace the `confirm_trade` function in `src/scorched/api/trades.py`:

```python
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import TradeRecommendation
from ..schemas import ConfirmTradeRequest, ConfirmTradeResponse, RejectTradeRequest, RejectTradeResponse
from ..services import portfolio as portfolio_svc
from ..broker import get_broker
from .deps import require_owner_pin

router = APIRouter(prefix="/trades", tags=["trades"])


@router.post("/confirm", response_model=ConfirmTradeResponse, dependencies=[Depends(require_owner_pin)])
async def confirm_trade(body: ConfirmTradeRequest, db: AsyncSession = Depends(get_db)):
    rec = (
        await db.execute(
            select(TradeRecommendation).where(TradeRecommendation.id == body.recommendation_id)
        )
    ).scalars().first()

    if rec is None:
        raise HTTPException(status_code=404, detail=f"Recommendation {body.recommendation_id} not found")
    if rec.status != "pending":
        raise HTTPException(status_code=409, detail=f"Recommendation is already {rec.status}")

    broker = get_broker(db)

    if rec.action == "buy":
        result = await broker.submit_buy(
            symbol=rec.symbol,
            qty=body.shares,
            limit_price=body.execution_price,
            recommendation_id=body.recommendation_id,
        )
    else:
        result = await broker.submit_sell(
            symbol=rec.symbol,
            qty=body.shares,
            limit_price=body.execution_price,
            recommendation_id=body.recommendation_id,
        )

    if result["status"] != "filled":
        raise HTTPException(
            status_code=422,
            detail=f"Order not filled: status={result['status']} for {rec.symbol}"
        )

    # Build response compatible with existing ConfirmTradeResponse schema
    from ..models import Position
    pos = (await db.execute(
        select(Position).where(Position.symbol == rec.symbol)
    )).scalars().first()

    from ..schemas import PositionDetail
    position_detail = None
    if pos:
        position_detail = PositionDetail(
            symbol=pos.symbol,
            shares=pos.shares,
            avg_cost_basis=pos.avg_cost_basis,
            first_purchase_date=pos.first_purchase_date,
        )

    return ConfirmTradeResponse(
        trade_id=result.get("trade_id", 0),
        symbol=rec.symbol,
        action=rec.action,
        shares=result["filled_qty"],
        execution_price=result["filled_avg_price"],
        total_value=(result["filled_qty"] * result["filled_avg_price"]).quantize(Decimal("0.01")),
        new_cash_balance=result.get("new_cash_balance", Decimal("0")),
        position=position_detail,
        realized_gain=result.get("realized_gain"),
        tax_category=result.get("tax_category"),
    )


@router.post("/{recommendation_id}/reject", response_model=RejectTradeResponse, dependencies=[Depends(require_owner_pin)])
async def reject_trade(
    recommendation_id: int,
    body: RejectTradeRequest,
    db: AsyncSession = Depends(get_db),
):
    rec = (
        await db.execute(
            select(TradeRecommendation).where(TradeRecommendation.id == recommendation_id)
        )
    ).scalars().first()

    if rec is None:
        raise HTTPException(status_code=404, detail=f"Recommendation {recommendation_id} not found")
    if rec.status != "pending":
        raise HTTPException(status_code=409, detail=f"Recommendation is already {rec.status}")

    rec.status = "rejected"
    await db.commit()
    return RejectTradeResponse(recommendation_id=recommendation_id, status="rejected")
```

- [ ] **Step 3: Run existing tests to verify no regression**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/ -v 2>&1 | tail -30`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/scorched/api/trades.py src/scorched/broker/__init__.py
git commit -m "feat: wire broker adapter into trade confirmation endpoint"
```

---

## Task 6: Phase 1.5 Cron Script — Circuit Breaker Gate

**Files:**
- Create: `cron/tradebot_phase1_5.py`

- [ ] **Step 1: Create the circuit breaker cron script**

Create `cron/tradebot_phase1_5.py`:

```python
#!/usr/bin/env python3
"""
Phase 1.5 — Circuit breaker gate (9:30 AM ET, Mon-Fri)

Reads Phase 1's recommendations JSON, runs circuit breaker checks,
filters out any buys that fail gate checks, writes a filtered
recommendations file for Phase 2, and sends gate results via Telegram.

Environment:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
              TRADEBOT_URL (optional, defaults to http://localhost:8000)
"""
import json
import os
import pathlib
import urllib.request
import datetime
import pytz

# Load .env from project root
_env_file = pathlib.Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RECS_FILE = "/tmp/tradebot_recommendations.json"
FILTERED_FILE = "/tmp/tradebot_recommendations.json"  # overwrites in place


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram env vars not set — skipping notification")
        return
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"Telegram sent: {resp.read().decode()[:120]}")
    except Exception as e:
        print(f"Telegram error: {e}")


def main():
    est_tz = pytz.timezone("America/New_York")
    now_est = datetime.datetime.now(est_tz)
    today_str = now_est.date().strftime("%Y-%m-%d")

    print(f"[{now_est.strftime('%Y-%m-%d %H:%M:%S %Z')}] Phase 1.5: circuit breaker for {today_str}")

    if not os.path.exists(RECS_FILE):
        print("No recommendations file found — nothing to gate.")
        return

    with open(RECS_FILE) as f:
        stored = json.load(f)

    if stored["date"] != today_str:
        print(f"Date mismatch: {stored['date']} != {today_str}")
        return

    recs = stored["recommendations"]
    if not recs:
        print("No recommendations to gate.")
        return

    # Import circuit breaker (needs project on sys.path)
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

    import asyncio
    from scorched.circuit_breaker import run_circuit_breaker
    from scorched.services.strategy import load_strategy

    strategy = load_strategy()
    cb_config = strategy.get("circuit_breaker", {"enabled": False})

    if not cb_config.get("enabled", False):
        print("Circuit breaker disabled — passing all recommendations through.")
        return

    # Run gate checks
    results = asyncio.run(run_circuit_breaker(recs, cb_config))

    passed = []
    blocked = []
    for rec in results:
        gate = rec.pop("gate_result")
        if gate.passed:
            passed.append(rec)
        else:
            blocked.append((rec, gate.reason))

    # Build Telegram message
    msg = f"TRADEBOT // {today_str} - Circuit Breaker\n"

    if blocked:
        msg += "\nBLOCKED:\n"
        for rec, reason in blocked:
            msg += f"  {rec['action'].upper()} {rec['symbol']} — {reason}\n"

    if passed:
        msg += "\nCLEARED:\n"
        for rec in passed:
            msg += f"  {rec['action'].upper()} {rec['symbol']}\n"
    else:
        msg += "\nAll buys blocked — no trades will execute.\n"

    send_telegram(msg)

    # Write filtered file for Phase 2
    stored["recommendations"] = passed
    stored["symbols"] = [r["symbol"] for r in passed]
    with open(FILTERED_FILE, "w") as f:
        json.dump(stored, f)

    print(f"Phase 1.5 complete: {len(passed)} passed, {len(blocked)} blocked.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x /home/ubuntu/tradebot/cron/tradebot_phase1_5.py`

- [ ] **Step 3: Commit**

```bash
git add cron/tradebot_phase1_5.py
git commit -m "feat: add Phase 1.5 circuit breaker cron script"
```

---

## Task 7: Broker Status Endpoint and Position Reconciliation

**Files:**
- Create: `src/scorched/api/broker_status.py`
- Modify: `src/scorched/main.py`

- [ ] **Step 1: Create broker status endpoint**

Create `src/scorched/api/broker_status.py`:

```python
"""Broker status and position reconciliation endpoint."""
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..broker import get_broker
from ..config import settings
from ..database import get_db
from ..models import Position

router = APIRouter(prefix="/broker", tags=["broker"])


@router.get("/status")
async def broker_status(db: AsyncSession = Depends(get_db)):
    """Return broker mode, account info, and position reconciliation."""
    broker = get_broker(db)
    account = await broker.get_account()

    result = {
        "broker_mode": settings.broker_mode,
        "account": account,
        "reconciliation": None,
    }

    # Position reconciliation: compare local DB vs broker
    if settings.broker_mode in ("alpaca_paper", "alpaca_live"):
        broker_positions = await broker.get_positions()
        local_positions = (await db.execute(select(Position))).scalars().all()

        broker_map = {p["symbol"]: p for p in broker_positions}
        local_map = {p.symbol: p for p in local_positions}

        all_symbols = set(broker_map.keys()) | set(local_map.keys())
        diffs = []

        for sym in sorted(all_symbols):
            b = broker_map.get(sym)
            l = local_map.get(sym)
            broker_qty = b["qty"] if b else Decimal("0")
            local_qty = l.shares if l else Decimal("0")

            if broker_qty != local_qty:
                diffs.append({
                    "symbol": sym,
                    "broker_qty": str(broker_qty),
                    "local_qty": str(local_qty),
                    "status": "MISMATCH",
                })
            else:
                diffs.append({
                    "symbol": sym,
                    "broker_qty": str(broker_qty),
                    "local_qty": str(local_qty),
                    "status": "OK",
                })

        result["reconciliation"] = {
            "positions": diffs,
            "has_mismatches": any(d["status"] == "MISMATCH" for d in diffs),
        }

    return result
```

- [ ] **Step 2: Register the router in main.py**

In `src/scorched/main.py`, add the import and router registration. After the line `from .api import costs, market, playbook, portfolio, recommendations, strategy, trades`, change to:

```python
from .api import broker_status, costs, market, playbook, portfolio, recommendations, strategy, trades
```

After the line `app.include_router(market.router, prefix="/api/v1")`, add:

```python
app.include_router(broker_status.router, prefix="/api/v1")
```

- [ ] **Step 3: Commit**

```bash
git add src/scorched/api/broker_status.py src/scorched/main.py
git commit -m "feat: add broker status endpoint with position reconciliation"
```

---

## Task 8: Update Phase 2 Cron for Broker-Aware Execution

**Files:**
- Modify: `cron/tradebot_phase2.py`

- [ ] **Step 1: Update Phase 2 to report broker mode and handle non-filled orders**

The changes to `cron/tradebot_phase2.py` are minimal — the broker routing happens server-side in the confirm endpoint. The cron script just needs to handle the new 422 error for unfilled orders and report broker mode.

In `cron/tradebot_phase2.py`, replace the `main()` function:

```python
def main():
    est_tz = pytz.timezone("America/New_York")
    now_est = datetime.datetime.now(est_tz)
    today_str = now_est.date().strftime("%Y-%m-%d")

    print(f"[{now_est.strftime('%Y-%m-%d %H:%M:%S %Z')}] Phase 2: confirming trades for {today_str}")

    if not os.path.exists(RECS_FILE):
        send_telegram(f"TRADEBOT // {today_str} - Phase 2 skipped: no Phase 1 data found.")
        print("No recommendations file found.")
        return

    with open(RECS_FILE) as f:
        stored = json.load(f)

    if stored["date"] != today_str:
        send_telegram(
            f"TRADEBOT // {today_str} - Phase 2 skipped: "
            f"recommendations are for {stored['date']}, not today."
        )
        os.remove(RECS_FILE)
        print(f"Date mismatch: {stored['date']} != {today_str}")
        return

    recs = stored["recommendations"]
    symbols = stored["symbols"]
    pending = recs

    if not pending:
        send_telegram(f"TRADEBOT // {today_str} - Phase 2: no trades to confirm.")
        os.remove(RECS_FILE)
        return

    # Fetch broker mode for reporting
    try:
        broker_info = http_get("/api/v1/broker/status")
        broker_mode = broker_info.get("broker_mode", "paper")
    except Exception:
        broker_mode = "paper"

    # Fetch opening prices (used as limit price for broker orders)
    try:
        qs = urllib.parse.urlencode({"symbols": ",".join(symbols), "date": today_str})
        prices_resp = http_get(f"/api/v1/market/opening-prices?{qs}")
        opening_prices = prices_resp.get("opening_prices", {})
    except Exception as e:
        print(f"Opening prices fetch failed: {e}")
        opening_prices = {}

    trades_detail = ""
    for r in pending:
        rec_id = r["id"]
        symbol = r["symbol"]
        action = r["action"].upper()
        qty = float(r["quantity"])
        suggested = float(r["suggested_price"])
        open_price = opening_prices.get(symbol)
        fill_price = open_price if open_price is not None else suggested

        try:
            result = http_post("/api/v1/trades/confirm", {
                "recommendation_id": rec_id,
                "execution_price": fill_price,
                "shares": qty,
            })
            print(f"confirm_trade {symbol}: {result}")
            if "error" in result:
                print(f"  skipping {symbol}: {result['error']}")
                continue
            gain = result.get("realized_gain")
            actual_price = float(result.get("execution_price", fill_price))
            slip = actual_price - suggested
            trades_detail += f"  {action} {symbol} - {qty:.0f}sh @ ${actual_price:.2f} (slippage: {'+' if slip>=0 else ''}{slip:.2f})\n"
            if gain is not None:
                gain_f = float(gain)
                trades_detail += f"    Realized P&L: {'+' if gain_f>=0 else ''}${gain_f:,.2f}\n"
        except urllib.error.HTTPError as e:
            body = e.read().decode() if hasattr(e, 'read') else str(e)
            print(f"confirm_trade {symbol} failed ({e.code}): {body}")
            trades_detail += f"  {action} {symbol} - NOT FILLED: {body[:100]}\n"
        except Exception as e:
            print(f"confirm_trade {symbol} failed: {e}")
            trades_detail += f"  {action} {symbol} - ERROR: {e}\n"

    # Fetch updated portfolio
    try:
        portfolio = http_get("/api/v1/portfolio")
        total = float(portfolio.get("total_value", 0))
        ret_pct = portfolio.get("all_time_return_pct", 0)
        cash = float(portfolio.get("cash_balance", 0))
        positions = portfolio.get("positions", [])
    except Exception as e:
        print(f"Portfolio fetch failed: {e}")
        portfolio = {}
        total = cash = 0
        ret_pct = 0
        positions = []

    mode_label = {"paper": "PAPER", "alpaca_paper": "ALPACA-PAPER", "alpaca_live": "LIVE"}.get(broker_mode, broker_mode.upper())
    msg = f"TRADEBOT [{mode_label}] // {today_str} - Executed at open\n"
    msg += f"Portfolio: ${total:,.2f} ({fmt_pct(ret_pct)})\n\n"
    msg += "Trades Executed:\n" + trades_detail

    if positions:
        msg += "\nOpen Positions:\n"
        for p in positions:
            gain = float(p.get("unrealized_gain", 0))
            gain_pct = float(p.get("unrealized_gain_pct", 0))
            tax = "ST" if "short" in p.get("tax_category", "") else "LT"
            sign = "+" if gain >= 0 else ""
            msg += (
                f"  {p['symbol']}: {float(p['shares']):.0f}sh | "
                f"avg ${float(p['avg_cost_basis']):.2f} | "
                f"now ${float(p['current_price']):.2f} | "
                f"{sign}${gain:,.2f} ({sign}{gain_pct:.1f}%) [{tax}]\n"
            )

    send_telegram(msg)
    os.remove(RECS_FILE)
    print("Phase 2 complete.")
```

- [ ] **Step 2: Run all tests to verify no regression**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/ -v 2>&1 | tail -30`
Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add cron/tradebot_phase2.py
git commit -m "feat: update Phase 2 cron for broker-aware execution and mode reporting"
```

---

## Task 9: Documentation and Cron Schedule Update

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md with Alpaca integration docs**

Add a new section after the "Gotchas" section in `CLAUDE.md`:

```markdown
## Broker Integration (Alpaca)

The system supports three broker modes, controlled by `BROKER_MODE` in `.env`:

| Mode | Behavior |
|------|----------|
| `paper` (default) | DB-only trades, no broker. Original behavior. |
| `alpaca_paper` | Orders go to Alpaca paper trading. Fills recorded in local DB. |
| `alpaca_live` | Orders go to Alpaca live trading. Real money. |

**Required env vars for Alpaca modes:**
```
BROKER_MODE=alpaca_paper
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
```

**Circuit Breaker (Phase 1.5):** Runs at 9:30 AM ET, between Phase 1 (recommendations) and Phase 2 (execution). Gates buy orders based on:
- Individual stock gap-down from prior close (default: >2%)
- Price drift from Claude's suggested price (default: >1.5%)
- SPY gap-down (default: >1%)
- VIX absolute level (default: >30) or overnight spike (default: >20%)

Thresholds are configurable in `strategy.json` under `circuit_breaker`. Sells always pass through.

**Daily Cron Schedule (UTC, after DST = UTC-4):**
- `30 12 * * 1-5` → Phase 1: Generate recommendations (8:30 AM ET)
- `30 13 * * 1-5` → Phase 1.5: Circuit breaker gate (9:30 AM ET) ← NEW
- `35 13 * * 1-5` → Phase 2: Execute trades (9:35 AM ET) ← shifted 10min later
- `02 20 * * 1-5` → Phase 3: EOD summary (4:02 PM ET)

**Position Reconciliation:** `GET /api/v1/broker/status` compares local DB positions against Alpaca holdings and flags mismatches.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add Alpaca broker integration and circuit breaker documentation"
```

---

## Summary of New Cron Schedule

After implementation, the VM crontab should be updated to:

```
30 12 * * 1-5  cd ~/tradebot && python3 cron/tradebot_phase1.py >> /tmp/tradebot_phase1.log 2>&1
30 13 * * 1-5  cd ~/tradebot && python3 cron/tradebot_phase1_5.py >> /tmp/tradebot_phase1_5.log 2>&1
35 13 * * 1-5  cd ~/tradebot && python3 cron/tradebot_phase2.py >> /tmp/tradebot_phase2.log 2>&1
02 20 * * 1-5  cd ~/tradebot && python3 cron/tradebot_phase3.py >> /tmp/tradebot_phase3.log 2>&1
```

## Go-Live Checklist (for when you're ready to flip from paper to live)

1. Run `alpaca_paper` for at least 2-4 weeks, comparing fills vs. old paper results
2. Verify position reconciliation shows no mismatches (`GET /api/v1/broker/status`)
3. Tune circuit breaker thresholds based on observed gate triggers
4. Set `STARTING_CAPITAL` to match actual Alpaca account balance
5. Change `BROKER_MODE=alpaca_live` in `.env` on VM
6. Monitor first live week with Telegram — check every fill report
