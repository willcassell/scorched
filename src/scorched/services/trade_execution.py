"""Shared trade execution helper — used by both REST /trades/confirm and MCP confirm_trade.

Single source of truth for the validate-and-submit contract: live price fetch,
drift check (buys only), gate re-run, broker submission, and response assembly.
Both transports call validate_and_submit_trade(); neither calls the broker directly.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..broker import get_broker
from ..config import settings
from ..models import Portfolio, Position, TradeRecommendation
from ..risk_gates import run_all_buy_gates
from ..services.strategy import load_strategy_json
from ..services.telegram import send_telegram

logger = logging.getLogger(__name__)


@dataclass
class TradeExecutionResult:
    """Structured result from validate_and_submit_trade.

    status is either "submitted" (Alpaca fire-and-forget) or "filled" (paper broker).
    Fields other than status/symbol/action may be zero/None when status=="submitted".
    """
    status: str
    symbol: str
    action: str
    filled_qty: Decimal
    filled_price: Decimal
    new_cash_balance: Decimal | None = None
    realized_gain: Decimal | None = None
    tax_category: str | None = None
    trade_id: int | None = None


def _fetch_live_price_single(symbol: str) -> Decimal | None:
    """Fetch current price via Alpaca snapshot (sell-path helper). Returns None on failure."""
    try:
        from ..services.alpaca_data import fetch_snapshots_sync
        snaps = fetch_snapshots_sync([symbol])
        if symbol in snaps:
            return Decimal(str(snaps[symbol]["current_price"]))
    except Exception:
        logger.debug("_fetch_live_price_single failed for %s", symbol, exc_info=True)
    return None


async def validate_and_submit_trade(rec_id: int, db: AsyncSession) -> TradeExecutionResult:
    """Server-decides trade execution with full gate re-run.

    Used by both REST /trades/confirm and MCP confirm_trade — single source of truth
    for the trade-submission contract. The server fetches live prices, re-runs all
    buy-side gates, computes the limit price from strategy buffers, and submits to the
    configured broker. Client-supplied price or quantity are NEVER used — the stored
    recommendation is authoritative.

    Raises:
        LookupError: recommendation not found.
        ValueError: terminal status, drift exceeded, gate rejected, or live price unavailable.
        RuntimeError: broker call failed (Telegram alert already sent).
    """
    from ..services.alpaca_data import fetch_snapshots_sync
    from ..services.recommender import _compute_portfolio_total_value, _get_sector_for_symbol

    rec = (
        await db.execute(
            select(TradeRecommendation).where(TradeRecommendation.id == rec_id)
        )
    ).scalars().first()

    if rec is None:
        raise LookupError(f"Recommendation {rec_id} not found")
    if rec.status not in ("pending", "submitted"):
        raise ValueError(f"already:{rec.status}")

    # Server decides everything — stored rec is authoritative.
    qty = Decimal(str(rec.quantity))
    stored_price = Decimal(str(rec.suggested_price))

    strategy = load_strategy_json()
    exec_cfg = strategy.get("execution", {})
    drift_tolerance_pct = Decimal(str(exec_cfg.get("confirm_drift_pct", 5.0)))

    if rec.action == "buy":
        portfolio = (await db.execute(select(Portfolio))).scalars().first()
        held = list((await db.execute(select(Position))).scalars().all())
        held_symbols = {p.symbol.upper() for p in held}

        all_symbols = list({rec.symbol.upper()} | {p.symbol.upper() for p in held})
        snapshots = await asyncio.to_thread(fetch_snapshots_sync, all_symbols)

        def _live_price_for(sym: str) -> Decimal:
            snap = snapshots.get(sym.upper())
            if snap and snap.get("current_price"):
                return Decimal(str(snap["current_price"]))
            return Decimal("0")

        live_price = _live_price_for(rec.symbol)
        if live_price <= 0:
            raise ValueError(f"live price:Cannot fetch live price for {rec.symbol}")

        drift_pct = abs(live_price - stored_price) / stored_price * 100
        if drift_pct > drift_tolerance_pct:
            raise ValueError(
                f"drift:{drift_pct:.1f}% exceeds {drift_tolerance_pct}% tolerance — "
                f"stored ${stored_price}, live ${live_price}"
            )

        price_data = {sym.upper(): {"current_price": float(_live_price_for(sym))} for sym in all_symbols}
        total_value = _compute_portfolio_total_value(
            Decimal(str(portfolio.cash_balance)),
            held,
            price_data,
        )

        existing_pos = next((p for p in held if p.symbol.upper() == rec.symbol.upper()), None)
        existing_value = (Decimal(str(existing_pos.shares)) * live_price) if existing_pos else Decimal("0")

        proposed_sector = await asyncio.to_thread(_get_sector_for_symbol, rec.symbol)
        held_sectors = await asyncio.gather(
            *(asyncio.to_thread(_get_sector_for_symbol, p.symbol) for p in held)
        )
        held_with_sector = [
            {
                "symbol": p.symbol,
                "sector": held_sectors[i],
                "market_value": Decimal(str(p.shares)) * _live_price_for(p.symbol),
            }
            for i, p in enumerate(held)
        ]

        conc = strategy.get("concentration", {})
        gate_result = run_all_buy_gates(
            symbol=rec.symbol,
            sector=proposed_sector,
            buy_notional=qty * live_price,
            current_cash=Decimal(str(portfolio.cash_balance)),
            total_portfolio_value=total_value,
            held_symbols=held_symbols,
            held_positions_with_sector=held_with_sector,
            existing_position_value=existing_value,
            reserve_pct=Decimal(str(settings.min_cash_reserve_pct)),
            max_position_pct=Decimal(str(conc.get("max_position_pct", 33))),
            max_sector_pct=float(conc.get("max_sector_pct", 40)),
            max_holdings=int(conc.get("max_holdings", 10)),
        )
        if not gate_result.passed:
            logger.warning(
                "Risk gate rejected at confirm time: symbol=%s rec_id=%s reason=%s",
                rec.symbol, rec_id, gate_result.reason,
            )
            raise ValueError(f"gate:{gate_result.reason}")
    else:
        # Sell path: fetch live price for limit price calculation only (no gates, no drift check).
        live_price = await asyncio.to_thread(_fetch_live_price_single, rec.symbol)
        if live_price is None:
            raise ValueError(f"live price:Cannot fetch live price for {rec.symbol}")

    # Compute final limit price using strategy buffer.
    if rec.action == "buy":
        buf = Decimal(str(exec_cfg.get("buy_limit_buffer_pct", 0.3))) / Decimal("100")
        limit_price = (live_price * (Decimal("1") + buf)).quantize(Decimal("0.01"))
    else:
        buf = Decimal(str(exec_cfg.get("sell_limit_buffer_pct", 0.3))) / Decimal("100")
        limit_price = (live_price * (Decimal("1") - buf)).quantize(Decimal("0.01"))

    broker = get_broker(db)
    try:
        if rec.action == "buy":
            result = await broker.submit_buy(
                symbol=rec.symbol,
                qty=qty,
                limit_price=limit_price,
                recommendation_id=rec_id,
            )
        else:
            result = await broker.submit_sell(
                symbol=rec.symbol,
                qty=qty,
                limit_price=limit_price,
                recommendation_id=rec_id,
            )
    except Exception as exc:
        logger.error(
            "Broker order failed: symbol=%s action=%s recommendation_id=%s error=%s",
            rec.symbol, rec.action, rec_id, exc,
            exc_info=True,
        )
        await send_telegram(
            f"🚨 BROKER ERROR — order may have filled on Alpaca!\n"
            f"Symbol: {rec.symbol}\n"
            f"Action: {rec.action}\n"
            f"Shares: {qty}\n"
            f"Recommendation ID: {rec_id}\n"
            f"Error: {exc}"
        )
        raise RuntimeError(
            f"Broker call failed for {rec.action} {rec.symbol} "
            f"(rec_id={rec_id}). "
            f"The order may have filled on Alpaca — check broker dashboard. "
            f"Error: {exc}"
        )

    if result["status"] == "submitted":
        rec.status = "submitted"
        await db.commit()
        return TradeExecutionResult(
            status="submitted",
            symbol=rec.symbol,
            action=rec.action,
            filled_qty=result["filled_qty"],
            filled_price=result["filled_avg_price"],
            new_cash_balance=Decimal("0"),
            realized_gain=None,
            tax_category=None,
            trade_id=0,
        )

    if result["status"] != "filled":
        raise ValueError(f"gate:Order not filled: status={result['status']} for {rec.symbol}")

    return TradeExecutionResult(
        status="filled",
        symbol=rec.symbol,
        action=rec.action,
        filled_qty=result["filled_qty"],
        filled_price=result["filled_avg_price"],
        new_cash_balance=result.get("new_cash_balance"),
        realized_gain=result.get("realized_gain"),
        tax_category=result.get("tax_category"),
        trade_id=result.get("trade_id", 0),
    )
