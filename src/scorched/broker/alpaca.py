"""Alpaca broker — submits real orders via alpaca-py SDK."""
import asyncio
import logging
import time
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


async def _record_api_call(db: AsyncSession, endpoint: str, status: str,
                           response_time_ms: int, error_message: str | None = None,
                           symbol: str | None = None, service: str = "alpaca_trade"):
    """Record an Alpaca API call to the api_call_log table."""
    try:
        from ..models import ApiCallLog
        db.add(ApiCallLog(
            service=service,
            endpoint=endpoint,
            status=status,
            response_time_ms=response_time_ms,
            error_message=error_message,
            symbol=symbol,
        ))
        await db.commit()
    except Exception:
        pass  # Don't let tracking failures break trading


class AlpacaBroker(BrokerAdapter):
    """Submits orders to Alpaca and records fills in the local DB.

    Uses limit orders by default (limit_price from caller). Falls back to
    market orders only if limit_price is None.

    Orders are fire-and-forget: submit_buy/sell submit the order and record
    it as a pending fill immediately.  A separate reconciliation step
    (reconcile_pending_orders) checks Alpaca for fills and updates the local
    DB.  This avoids blocking Phase 2 with polling timeouts.
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
        client_oid = f"scorched-{recommendation_id}-{symbol}-buy" if recommendation_id else None
        order_data = LimitOrderRequest(
            symbol=symbol,
            qty=float(qty),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=float(limit_price),
            client_order_id=client_oid,
        )

        start = time.monotonic()
        try:
            order = await self._submit_order_with_retry(order_data)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            await _record_api_call(self.db, "submit_buy", "success", elapsed_ms, symbol=symbol)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            await _record_api_call(self.db, "submit_buy", "error", elapsed_ms,
                                   error_message=str(exc)[:500], symbol=symbol)
            raise

        order_id = str(order.id)
        logger.info("Submitted BUY %s x%s limit=$%s — order_id=%s", symbol, qty, limit_price, order_id)

        # Record as pending — reconcile_pending_orders will check for fills later
        write_pending_fill(
            order_id=order_id,
            symbol=symbol,
            action="buy",
            qty=qty,
            fill_price=limit_price,  # placeholder until actual fill
            recommendation_id=recommendation_id,
        )

        return {
            "status": "submitted",
            "filled_qty": Decimal("0"),
            "filled_avg_price": limit_price,
            "symbol": symbol,
            "order_id": order_id,
            "trade_id": None,
            "new_cash_balance": None,
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
            from ..config import settings as _settings
            if _settings.broker_mode == "alpaca_live":
                raise ValueError(
                    f"SELL rejected for {symbol}: no position on Alpaca (live mode). "
                    f"Cannot fall back to paper broker — resolve manually."
                )
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
        client_oid = f"scorched-{recommendation_id}-{symbol}-sell" if recommendation_id else None
        order_data = LimitOrderRequest(
            symbol=symbol,
            qty=float(qty),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=float(limit_price),
            client_order_id=client_oid,
        )

        start = time.monotonic()
        try:
            order = await self._submit_order_with_retry(order_data)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            await _record_api_call(self.db, "submit_sell", "success", elapsed_ms, symbol=symbol)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            await _record_api_call(self.db, "submit_sell", "error", elapsed_ms,
                                   error_message=str(exc)[:500], symbol=symbol)
            raise

        order_id = str(order.id)
        logger.info("Submitted SELL %s x%s limit=$%s — order_id=%s", symbol, qty, limit_price, order_id)

        # Record as pending — reconcile_pending_orders will check for fills later
        write_pending_fill(
            order_id=order_id,
            symbol=symbol,
            action="sell",
            qty=qty,
            fill_price=limit_price,  # placeholder until actual fill
            recommendation_id=recommendation_id,
        )

        return {
            "status": "submitted",
            "filled_qty": Decimal("0"),
            "filled_avg_price": limit_price,
            "symbol": symbol,
            "order_id": order_id,
            "trade_id": None,
            "new_cash_balance": None,
            "realized_gain": None,
            "tax_category": None,
        }

    async def get_positions(self) -> list[dict]:
        loop = asyncio.get_running_loop()
        start = time.monotonic()
        try:
            positions = await loop.run_in_executor(None, self.client.get_all_positions)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            await _record_api_call(self.db, "get_positions", "success", elapsed_ms)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            await _record_api_call(self.db, "get_positions", "error", elapsed_ms,
                                   error_message=str(exc)[:500])
            raise
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
        start = time.monotonic()
        try:
            account = await loop.run_in_executor(None, self.client.get_account)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            await _record_api_call(self.db, "get_account", "success", elapsed_ms)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            await _record_api_call(self.db, "get_account", "error", elapsed_ms,
                                   error_message=str(exc)[:500])
            raise
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


async def reconcile_pending_orders(db: AsyncSession) -> list[dict]:
    """Check all pending orders on Alpaca and record fills in local DB.

    Returns a list of reconciliation results for each pending order.
    Called by the reconcile cron job ~15 min after Phase 2.
    """
    from .pending_fills import get_pending_fills, remove_pending_fill
    from ..config import settings

    if settings.broker_mode not in ("alpaca_paper", "alpaca_live"):
        return []

    is_paper = settings.broker_mode == "alpaca_paper"
    client = TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=is_paper,
    )

    pending = get_pending_fills()
    if not pending:
        logger.info("No pending orders to reconcile")
        return []

    logger.info("Reconciling %d pending orders", len(pending))
    loop = asyncio.get_running_loop()
    results = []

    for fill in pending:
        order_id = fill["order_id"]
        symbol = fill["symbol"]
        action = fill["action"]
        recommendation_id = fill.get("recommendation_id")

        try:
            order = await loop.run_in_executor(
                None, lambda oid=order_id: client.get_order_by_id(order_id=oid)
            )
            status = order.status.value if hasattr(order.status, 'value') else str(order.status)

            if status == "filled":
                filled_qty = Decimal(str(order.filled_qty))
                filled_price = Decimal(str(order.filled_avg_price))

                if action == "buy":
                    result = await apply_buy(
                        db,
                        recommendation_id=recommendation_id,
                        symbol=symbol,
                        shares=filled_qty,
                        execution_price=filled_price,
                        executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                    remove_pending_fill(order_id)
                    results.append({
                        "symbol": symbol,
                        "action": action,
                        "status": "filled",
                        "filled_qty": str(filled_qty),
                        "filled_price": str(filled_price),
                        "trade_id": result.trade_id,
                    })
                    logger.info("Reconciled BUY %s: %s shares @ $%s", symbol, filled_qty, filled_price)

                elif action == "sell":
                    result = await apply_sell(
                        db,
                        recommendation_id=recommendation_id,
                        symbol=symbol,
                        shares=filled_qty,
                        execution_price=filled_price,
                        executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                    remove_pending_fill(order_id)
                    results.append({
                        "symbol": symbol,
                        "action": action,
                        "status": "filled",
                        "filled_qty": str(filled_qty),
                        "filled_price": str(filled_price),
                        "trade_id": result.trade_id,
                        "realized_gain": str(result.realized_gain) if result.realized_gain else None,
                    })
                    logger.info("Reconciled SELL %s: %s shares @ $%s", symbol, filled_qty, filled_price)

            elif status in ("canceled", "expired", "rejected"):
                # Terminal non-fill — clean up pending record, mark rec as pending again
                remove_pending_fill(order_id)
                results.append({
                    "symbol": symbol,
                    "action": action,
                    "status": status,
                    "filled_qty": "0",
                    "filled_price": None,
                })
                logger.info("Order %s for %s %s reached terminal: %s", order_id, action, symbol, status)

            else:
                # Still open (new, accepted, partially_filled, etc.)
                results.append({
                    "symbol": symbol,
                    "action": action,
                    "status": f"still_open ({status})",
                    "filled_qty": str(order.filled_qty or 0),
                    "filled_price": None,
                })
                logger.info("Order %s for %s %s still open: %s", order_id, action, symbol, status)

        except Exception as exc:
            logger.error("Failed to reconcile order %s for %s: %s", order_id, symbol, exc, exc_info=True)
            results.append({
                "symbol": symbol,
                "action": action,
                "status": f"error: {exc}",
            })

    return results
