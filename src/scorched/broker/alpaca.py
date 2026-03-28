"""Alpaca broker — submits real orders via alpaca-py SDK."""
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from ..models import Portfolio, Position, TradeHistory, TradeRecommendation
from ..tax import classify_gain, estimate_tax
from .base import BrokerAdapter

logger = logging.getLogger(__name__)


class AlpacaBroker(BrokerAdapter):
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

        filled_order = await self._wait_for_fill(str(order.id), timeout=60)

        status = filled_order.status.value if hasattr(filled_order.status, 'value') else str(filled_order.status)
        filled_qty = Decimal(str(filled_order.filled_qty)) if filled_order.filled_qty else Decimal("0")
        filled_price = Decimal(str(filled_order.filled_avg_price)) if filled_order.filled_avg_price else limit_price

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

        pos = (await self.db.execute(
            select(Position).where(Position.symbol == symbol)
        )).scalars().first()

        if pos is None:
            pos = Position(
                symbol=symbol,
                shares=qty,
                avg_cost_basis=price,
                first_purchase_date=datetime.now(timezone.utc).date(),
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
            executed_at=datetime.now(timezone.utc),
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
        tax_cat = classify_gain(pos.first_purchase_date, datetime.now(timezone.utc).date()) if pos else "short_term"

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
            executed_at=datetime.now(timezone.utc),
            realized_gain=realized_gain,
            tax_category=tax_cat,
        )
        self.db.add(history)
        await self.db.commit()
        return history.id, portfolio.cash_balance, realized_gain, tax_cat
