"""Paper broker — wraps existing DB portfolio logic."""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Portfolio, Position
from ..services.portfolio import apply_buy, apply_sell
from .base import BrokerAdapter


class PaperBroker(BrokerAdapter):
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
            executed_at=datetime.now(timezone.utc),
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
            executed_at=datetime.now(timezone.utc),
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
                "market_value": None,
                "unrealized_pl": None,
            }
            for p in rows
        ]

    async def get_account(self) -> dict:
        portfolio = (await self.db.execute(select(Portfolio))).scalars().first()
        return {
            "cash": portfolio.cash_balance,
            "buying_power": portfolio.cash_balance,
            "equity": portfolio.cash_balance,
            "status": "ACTIVE",
        }

    async def get_order_status(self, order_id: str) -> dict:
        return {
            "order_id": order_id,
            "status": "filled",
            "filled_qty": None,
            "filled_avg_price": None,
        }
