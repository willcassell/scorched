"""Alpaca broker — submits real orders via alpaca-py SDK."""
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from ..services.portfolio import apply_buy, apply_sell
from .base import BrokerAdapter
from .pending_fills import write_pending_fill, remove_pending_fill

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

    async def _submit_order_with_retry(self, order_data, max_retries=1):
        """Submit order via executor with retry on transient (non-4xx) failures."""
        loop = asyncio.get_running_loop()
        last_exc = None
        for attempt in range(max_retries + 1):
            try:
                return await loop.run_in_executor(None, self._submit_order_sync, order_data)
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc).lower()
                # Don't retry client errors (4xx)
                if any(code in exc_str for code in ("400", "401", "403", "404", "422")):
                    raise
                if attempt < max_retries:
                    logger.warning("Alpaca order attempt %d failed, retrying in 3s: %s", attempt + 1, exc)
                    await asyncio.sleep(3)
        raise last_exc

    def _get_order_sync(self, order_id: str):
        return self.client.get_order_by_id(order_id=order_id)

    async def submit_buy(
        self,
        symbol: str,
        qty: Decimal,
        limit_price: Decimal,
        recommendation_id: int | None,
    ) -> dict:
        limit_price = Decimal(str(limit_price)).quantize(Decimal("0.01"))
        order_data = LimitOrderRequest(
            symbol=symbol,
            qty=float(qty),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=float(limit_price),
        )

        order = await self._submit_order_with_retry(order_data)

        filled_order = await self._wait_for_fill(str(order.id), timeout=60)

        status = filled_order.status.value if hasattr(filled_order.status, 'value') else str(filled_order.status)
        filled_qty = Decimal(str(filled_order.filled_qty)) if filled_order.filled_qty else Decimal("0")
        filled_price = Decimal(str(filled_order.filled_avg_price)) if filled_order.filled_avg_price else limit_price

        trade_id = None
        new_cash = None
        if status == "filled" and filled_qty > 0:
            # Write pending fill for crash recovery before DB recording
            write_pending_fill(
                order_id=str(order.id),
                symbol=symbol,
                action="buy",
                qty=filled_qty,
                fill_price=filled_price,
                recommendation_id=recommendation_id,
            )
            result = await apply_buy(
                self.db,
                recommendation_id=recommendation_id,
                symbol=symbol,
                shares=filled_qty,
                execution_price=filled_price,
                executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            trade_id = result.trade_id
            new_cash = result.new_cash_balance
            remove_pending_fill(str(order.id))

        return {
            "status": status,
            "filled_qty": filled_qty,
            "filled_avg_price": filled_price,
            "symbol": symbol,
            "order_id": str(order.id),
            "trade_id": trade_id,
            "new_cash_balance": new_cash,
        }

    def _get_position_sync(self, symbol: str):
        """Get a single position from Alpaca. Returns None if not held."""
        try:
            return self.client.get_open_position(symbol)
        except Exception as exc:
            # Alpaca returns 404 / 40410000 when position doesn't exist
            exc_str = str(exc).lower()
            if "not found" in exc_str or "404" in exc_str or "40410000" in exc_str:
                return None
            logger.warning("Unexpected error fetching Alpaca position for %s: %s", symbol, exc)
            raise

    async def submit_sell(
        self,
        symbol: str,
        qty: Decimal,
        limit_price: Decimal,
        recommendation_id: int | None,
    ) -> dict:
        # Guard: verify position exists on Alpaca to prevent accidental shorts
        loop = asyncio.get_running_loop()
        alpaca_pos = await loop.run_in_executor(None, self._get_position_sync, symbol)
        if alpaca_pos is None:
            logger.warning(
                "Sell rejected for %s: no position held on Alpaca (would create short)", symbol
            )
            # Fall back to paper broker for DB-only sell of legacy positions
            from .paper import PaperBroker
            paper = PaperBroker(self.db)
            return await paper.submit_sell(symbol, qty, limit_price, recommendation_id)

        # Cap sell qty at what Alpaca actually holds to prevent partial shorts
        alpaca_qty = Decimal(str(alpaca_pos.qty))
        if qty > alpaca_qty:
            logger.warning(
                "Sell qty %s > Alpaca holding %s for %s — capping to Alpaca qty",
                qty, alpaca_qty, symbol,
            )
            qty = alpaca_qty

        limit_price = Decimal(str(limit_price)).quantize(Decimal("0.01"))
        order_data = LimitOrderRequest(
            symbol=symbol,
            qty=float(qty),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=float(limit_price),
        )

        order = await self._submit_order_with_retry(order_data)

        filled_order = await self._wait_for_fill(str(order.id), timeout=60)

        status = filled_order.status.value if hasattr(filled_order.status, 'value') else str(filled_order.status)
        filled_qty = Decimal(str(filled_order.filled_qty)) if filled_order.filled_qty else Decimal("0")
        filled_price = Decimal(str(filled_order.filled_avg_price)) if filled_order.filled_avg_price else limit_price

        realized_gain = None
        tax_category = None
        trade_id = None
        new_cash = None
        if status == "filled" and filled_qty > 0:
            # Write pending fill for crash recovery before DB recording
            write_pending_fill(
                order_id=str(order.id),
                symbol=symbol,
                action="sell",
                qty=filled_qty,
                fill_price=filled_price,
                recommendation_id=recommendation_id,
            )
            result = await apply_sell(
                self.db,
                recommendation_id=recommendation_id,
                symbol=symbol,
                shares=filled_qty,
                execution_price=filled_price,
                executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            trade_id = result.trade_id
            new_cash = result.new_cash_balance
            realized_gain = result.realized_gain
            tax_category = result.tax_category
            remove_pending_fill(str(order.id))

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
        loop = asyncio.get_running_loop()
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
        loop = asyncio.get_running_loop()
        account = await loop.run_in_executor(None, self.client.get_account)
        return {
            "cash": account.cash,
            "buying_power": account.buying_power,
            "equity": account.equity,
            "status": account.status,
        }

    async def get_order_status(self, order_id: str) -> dict:
        loop = asyncio.get_running_loop()
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
        loop = asyncio.get_running_loop()
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
