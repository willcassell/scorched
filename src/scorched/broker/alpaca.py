"""Alpaca broker — submits real orders via alpaca-py SDK."""
import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from ..models import TradeHistory
from ..services.portfolio import apply_buy, apply_sell
from .base import BrokerAdapter
from .pending_fills import (
    write_pending_fill,
    update_pending_fill_order_id,
    remove_pending_fill,
    remove_pending_fill_by_client_oid,
    get_pending_fills,
)

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

    _TERMINAL_ORDER_STATUSES = ("filled", "canceled", "expired", "rejected", "done_for_day")

    async def _rename_pending_fill_coid(self, old_coid: str, new_coid: str) -> None:
        """Re-key a pending fill row's client_order_id (fresh-oid resubmission)."""
        from ..models import PendingFill
        row = (await self.db.execute(
            select(PendingFill).where(PendingFill.client_order_id == old_coid)
        )).scalars().first()
        if row is not None:
            row.client_order_id = new_coid
            await self.db.commit()

    async def _submit_order_with_retry(
        self, order_data, max_retries=1, allow_fresh_oid_on_terminal=False,
    ):
        """Submit order via executor with retry on transient (non-4xx) failures.

        Special-cases Alpaca error 40010001 ("client_order_id must be unique"):
        when our deterministic client_order_id collides, we fetch the existing
        order and return it as if newly submitted. This delivers true
        idempotency for Phase 2 confirm retries and intraday re-fires of the
        same deterministic key — Alpaca's API rejects duplicates instead of
        returning the prior order, so the recovery has to be explicit here.

        `allow_fresh_oid_on_terminal` (intraday day-scoped oids only): if the
        recovered order is already TERMINAL, this collision is a NEW intent
        reusing a consumed day-scoped key (e.g. afternoon exit_full after a
        morning exit_partial already filled and was reconciled). Returning the
        stale order would (a) silently skip the intended exit and (b) let the
        reconciler re-apply the morning's fill. Instead we resubmit with a
        fresh suffixed client_order_id. Rec-scoped oids keep the old behavior:
        a terminal recovered order IS the same intent, already handled.
        """
        loop = asyncio.get_running_loop()
        last_exc = None
        for attempt in range(max_retries + 1):
            try:
                return await loop.run_in_executor(None, self._submit_order_sync, order_data)
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc).lower()
                if "40010001" in exc_str or "client_order_id must be unique" in exc_str:
                    coid = getattr(order_data, "client_order_id", None)
                    if coid:
                        logger.warning(
                            "Alpaca rejected duplicate client_order_id=%s — "
                            "fetching existing order (idempotent recovery)",
                            coid,
                        )
                        try:
                            recovered = await loop.run_in_executor(
                                None,
                                lambda c=coid: self.client.get_order_by_client_id(c),
                            )
                        except Exception as lookup_exc:
                            logger.error(
                                "Idempotent recovery failed for client_order_id=%s: %s",
                                coid, lookup_exc,
                            )
                            raise exc from lookup_exc

                        rec_status = (
                            recovered.status.value
                            if hasattr(recovered.status, "value")
                            else str(recovered.status)
                        )
                        if (
                            allow_fresh_oid_on_terminal
                            and rec_status in self._TERMINAL_ORDER_STATUSES
                        ):
                            fresh_coid = f"{coid}-r{int(time.time())}"
                            logger.warning(
                                "Recovered order for %s is already %s — consumed "
                                "day-scoped key; resubmitting NEW intent with "
                                "client_order_id=%s",
                                coid, rec_status, fresh_coid,
                            )
                            # Re-key the pending row FIRST so a crash between
                            # resubmit and order-id update still reconciles the
                            # new order, not the stale terminal one.
                            await self._rename_pending_fill_coid(coid, fresh_coid)
                            order_data.client_order_id = fresh_coid
                            try:
                                return await loop.run_in_executor(
                                    None, self._submit_order_sync, order_data
                                )
                            except Exception:
                                # Restore the original key so the caller's
                                # cleanup (remove by original coid) still works.
                                await self._rename_pending_fill_coid(fresh_coid, coid)
                                raise
                        return recovered
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

        # Write pending fill BEFORE submitting to Alpaca — survives crashes
        await write_pending_fill(
            self.db,
            client_order_id=client_oid,
            symbol=symbol,
            action="buy",
            qty=qty,
            limit_price=limit_price,
            recommendation_id=recommendation_id,
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
            # Clean up the pre-written pending fill on submission failure
            if client_oid:
                await remove_pending_fill_by_client_oid(self.db, client_oid)
            raise

        order_id = str(order.id)
        logger.info("Submitted BUY %s x%s limit=$%s — order_id=%s", symbol, qty, limit_price, order_id)

        # Update pending fill with real Alpaca order ID
        if client_oid:
            await update_pending_fill_order_id(self.db, client_order_id=client_oid, order_id=order_id)

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
        _client_order_id_override: str | None = None,
        exit_reason: str | None = None,
        exit_trigger: str | None = None,
    ) -> dict:
        # Guard: verify position exists on Alpaca to prevent accidental shorts
        loop = asyncio.get_running_loop()
        alpaca_pos = await loop.run_in_executor(None, self._get_position_sync, symbol)
        if alpaca_pos is None:
            from ..config import settings as _settings
            if _settings.broker_mode == "alpaca_live" or not _settings.allow_paper_fallback_sell:
                mode = "live mode" if _settings.broker_mode == "alpaca_live" else "paper fallback disabled"
                raise ValueError(
                    f"SELL rejected for {symbol}: no position on Alpaca ({mode}). "
                    f"Resolve manually or set ALLOW_PAPER_FALLBACK_SELL=true for legacy DB-only paper sells."
                )
            logger.warning(
                "Sell rejected for %s: no position held on Alpaca (would create short)", symbol
            )
            # Silent paper-fallback sells cause cumulative cash drift vs Alpaca
            # (local DB records the trade; Alpaca never saw the position). Make
            # these loud so the operator can see them in real time.
            try:
                from ..services.telegram import send_telegram
                await send_telegram(
                    "⚠️ Paper-fallback sell\n"
                    f"Symbol: {symbol} (qty {qty} @ ${limit_price})\n"
                    "Position exists in local DB but NOT on Alpaca.\n"
                    "Selling via PaperBroker — Alpaca will not see this trade.\n"
                    "This WILL drift local cash vs Alpaca until next reconcile."
                )
            except Exception:  # noqa: BLE001 — Telegram is best-effort
                logger.exception("Failed to send paper-fallback Telegram alert")
            # Fall back to paper broker for DB-only sell of legacy positions
            from .paper import PaperBroker
            paper = PaperBroker(self.db)
            return await paper.submit_sell(
                symbol, qty, limit_price, recommendation_id,
                exit_reason=exit_reason, exit_trigger=exit_trigger,
            )

        # Cap sell qty at what Alpaca actually holds to prevent partial shorts
        alpaca_qty = Decimal(str(alpaca_pos.qty))
        if qty > alpaca_qty:
            logger.warning(
                "Sell qty %s > Alpaca holding %s for %s — capping to Alpaca qty",
                qty, alpaca_qty, symbol,
            )
            qty = alpaca_qty

        limit_price = Decimal(str(limit_price)).quantize(Decimal("0.01"))
        client_oid = _client_order_id_override or (
            f"scorched-{recommendation_id}-{symbol}-sell" if recommendation_id else None
        )
        order_data = LimitOrderRequest(
            symbol=symbol,
            qty=float(qty),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=float(limit_price),
            client_order_id=client_oid,
        )

        # Write pending fill BEFORE submitting to Alpaca — survives crashes
        await write_pending_fill(
            self.db,
            client_order_id=client_oid,
            symbol=symbol,
            action="sell",
            qty=qty,
            limit_price=limit_price,
            recommendation_id=recommendation_id,
            exit_reason=exit_reason,
            exit_trigger=exit_trigger,
        )

        start = time.monotonic()
        try:
            # Override oids are day-scoped intraday keys — allow fresh-oid
            # resubmission when the recovered colliding order is terminal.
            order = await self._submit_order_with_retry(
                order_data,
                allow_fresh_oid_on_terminal=bool(_client_order_id_override),
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)
            await _record_api_call(self.db, "submit_sell", "success", elapsed_ms, symbol=symbol)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            await _record_api_call(self.db, "submit_sell", "error", elapsed_ms,
                                   error_message=str(exc)[:500], symbol=symbol)
            # Clean up the pre-written pending fill on submission failure
            if client_oid:
                await remove_pending_fill_by_client_oid(self.db, client_oid)
            raise

        order_id = str(order.id)
        logger.info("Submitted SELL %s x%s limit=$%s — order_id=%s", symbol, qty, limit_price, order_id)

        # Update pending fill with real Alpaca order ID. Use the order's OWN
        # client_order_id — on fresh-oid resubmission the pending row was
        # re-keyed and the original client_oid no longer matches.
        effective_coid = str(getattr(order, "client_order_id", None) or client_oid or "")
        if effective_coid:
            await update_pending_fill_order_id(self.db, client_order_id=effective_coid, order_id=order_id)

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


# Serializes reconcile runs. Reachable from four entry points (startup, Phase 2
# pre-trade flush, Phase 2.5/2.75 crons via HTTP, manual /trades/reconcile) —
# all in this one FastAPI process, so an in-process lock is sufficient. Two
# overlapping runs would both read the same pending list and could double-apply
# fills for recommendation_id=None (intraday) sells.
_reconcile_lock = asyncio.Lock()


async def reconcile_pending_orders(db: AsyncSession) -> list[dict]:
    """Check all pending orders on Alpaca and record fills in local DB.

    Returns a list of reconciliation results for each pending order.
    Called by the reconcile cron job ~30 min after Phase 2.
    """
    from ..config import settings

    if settings.broker_mode not in ("alpaca_paper", "alpaca_live"):
        return []

    if _reconcile_lock.locked():
        logger.info("Reconcile already in progress — skipping concurrent run")
        return []

    async with _reconcile_lock:
        return await _reconcile_pending_orders_inner(db)


async def _reconcile_pending_orders_inner(db: AsyncSession) -> list[dict]:
    from ..config import settings

    is_paper = settings.broker_mode == "alpaca_paper"
    client = TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=is_paper,
    )

    pending = await get_pending_fills(db)
    if not pending:
        logger.info("No pending orders to reconcile")
        return []

    logger.info("Reconciling %d pending orders", len(pending))
    loop = asyncio.get_running_loop()
    results = []

    for fill in pending:
        order_id = fill["order_id"]
        client_oid = fill.get("client_order_id")
        symbol = fill["symbol"]
        action = fill["action"]
        recommendation_id = fill.get("recommendation_id")

        # If we crashed before getting the Alpaca order_id, look up by client_order_id
        if not order_id and client_oid:
            try:
                order = await loop.run_in_executor(
                    None, lambda coid=client_oid: client.get_order_by_client_id(coid)
                )
                order_id = str(order.id)
                await update_pending_fill_order_id(db, client_order_id=client_oid, order_id=order_id)
                logger.info("Recovered order_id=%s from client_oid=%s", order_id, client_oid)
            except Exception as exc:
                # Order was never submitted to Alpaca (crashed before submission)
                logger.info("No Alpaca order found for client_oid=%s — cleaning up: %s", client_oid, exc)
                await remove_pending_fill_by_client_oid(db, client_oid)
                results.append({
                    "symbol": symbol, "action": action,
                    "status": "never_submitted", "filled_qty": "0", "filled_price": None,
                })
                continue

        if not order_id:
            logger.warning("Pending fill for %s has no order_id or client_order_id — skipping", symbol)
            continue

        try:
            order = await loop.run_in_executor(
                None, lambda oid=order_id: client.get_order_by_id(order_id=oid)
            )
            status = order.status.value if hasattr(order.status, 'value') else str(order.status)

            if status == "filled":
                filled_qty = Decimal(str(order.filled_qty))
                filled_price = Decimal(str(order.filled_avg_price))

                # Idempotency guard: if a prior reconcile already wrote the
                # trade (commit succeeded on apply_*, but remove_pending_fill
                # lost the race), skip the re-apply and just clean up the
                # orphaned pending_fill. Without this, the unique constraint
                # on trade_history.recommendation_id keeps throwing on every
                # reconcile.
                if recommendation_id is not None:
                    existing = (await db.execute(
                        select(TradeHistory).where(
                            TradeHistory.recommendation_id == recommendation_id
                        )
                    )).scalars().first()
                    if existing is not None:
                        logger.info(
                            "Trade already recorded for rec %d (%s %s) — "
                            "cleaning up orphan pending_fill",
                            recommendation_id, action, symbol,
                        )
                        await remove_pending_fill(db, order_id)
                        await db.commit()
                        results.append({
                            "symbol": symbol, "action": action,
                            "status": "already_recorded",
                            "filled_qty": str(existing.shares),
                            "filled_price": str(existing.execution_price),
                            "trade_id": existing.id,
                        })
                        continue

                if action == "buy":
                    # enforce_cash=False: the order already filled on Alpaca —
                    # the money is spent. Raising on local cash here would leave
                    # the fill unrecorded (ghost position, missing TradeHistory).
                    result = await apply_buy(
                        db,
                        recommendation_id=recommendation_id,
                        symbol=symbol,
                        shares=filled_qty,
                        execution_price=filled_price,
                        executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        enforce_cash=False,
                    )
                    await remove_pending_fill(db, order_id)
                    await db.commit()
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
                        exit_reason=fill.get("exit_reason"),
                        exit_trigger=fill.get("exit_trigger"),
                    )
                    await remove_pending_fill(db, order_id)
                    await db.commit()
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
                # Check for partial fills before cleaning up (#3)
                filled_qty_raw = order.filled_qty
                filled_qty = Decimal(str(filled_qty_raw)) if filled_qty_raw else Decimal("0")

                if filled_qty > 0:
                    # Partial fill on a now-terminal order — record the filled portion
                    filled_price = Decimal(str(order.filled_avg_price))
                    logger.warning(
                        "Order %s for %s %s is %s with partial fill: %s shares @ $%s",
                        order_id, action, symbol, status, filled_qty, filled_price,
                    )
                    if action == "buy":
                        result = await apply_buy(
                            db,
                            recommendation_id=recommendation_id,
                            symbol=symbol,
                            shares=filled_qty,
                            execution_price=filled_price,
                            executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                            enforce_cash=False,
                        )
                    elif action == "sell":
                        result = await apply_sell(
                            db,
                            recommendation_id=recommendation_id,
                            symbol=symbol,
                            shares=filled_qty,
                            execution_price=filled_price,
                            executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                            exit_reason=fill.get("exit_reason"),
                            exit_trigger=fill.get("exit_trigger"),
                        )
                    await remove_pending_fill(db, order_id)
                    await db.commit()
                    results.append({
                        "symbol": symbol,
                        "action": action,
                        "status": f"partial_fill ({status})",
                        "filled_qty": str(filled_qty),
                        "filled_price": str(filled_price),
                    })
                else:
                    # Terminal with zero fill — clean up and mark rec as rejected (#13)
                    if recommendation_id:
                        from ..models import TradeRecommendation
                        rec_row = (await db.execute(
                            select(TradeRecommendation).where(TradeRecommendation.id == recommendation_id)
                        )).scalars().first()
                        if rec_row and rec_row.status == "submitted":
                            rec_row.status = "rejected"
                            logger.info("Marked rec %d as rejected (order %s)", recommendation_id, status)
                    await remove_pending_fill(db, order_id)
                    await db.commit()
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
