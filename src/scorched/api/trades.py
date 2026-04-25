import asyncio
import json
import logging
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import Portfolio, Position, TradeRecommendation
from ..schemas import ConfirmTradeRequest, ConfirmTradeResponse, PositionDetail, RejectTradeRequest, RejectTradeResponse
from ..broker import get_broker
from ..config import settings
from ..risk_gates import run_all_buy_gates
from ..services.telegram import send_telegram
from .deps import require_owner_pin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trades", tags=["trades"])

PRICE_DRIFT_TOLERANCE_PCT = Decimal("5.0")

# strategy.json is volume-mounted at /strategy.json (Docker) or falls back to
# the src root for local runs.
_STRATEGY_PATHS = [
    Path("/strategy.json"),
    Path(__file__).resolve().parents[4] / "strategy.json",
]


def _load_strategy() -> dict:
    for p in _STRATEGY_PATHS:
        if p.exists():
            with open(p) as f:
                return json.load(f)
    return {}


def fetch_live_price(symbol: str) -> Decimal | None:
    """Fetch current price via Alpaca snapshot. Returns None on failure."""
    try:
        from ..services.alpaca_data import fetch_snapshots_sync
        snaps = fetch_snapshots_sync([symbol])
        if symbol in snaps:
            return Decimal(str(snaps[symbol]["current_price"]))
    except Exception:
        logger.debug("fetch_live_price failed for %s", symbol, exc_info=True)
    return None


@router.post("/confirm", response_model=ConfirmTradeResponse, dependencies=[Depends(require_owner_pin)])
async def confirm_trade(body: ConfirmTradeRequest, db: AsyncSession = Depends(get_db)):
    rec = (
        await db.execute(
            select(TradeRecommendation).where(TradeRecommendation.id == body.recommendation_id)
        )
    ).scalars().first()

    if rec is None:
        raise HTTPException(status_code=404, detail=f"Recommendation {body.recommendation_id} not found")
    if rec.status not in ("pending", "submitted"):
        raise HTTPException(status_code=409, detail=f"Recommendation is already {rec.status}")

    # Server decides everything — client shares/price are advisory only.
    qty = Decimal(str(rec.quantity))
    stored_price = Decimal(str(rec.suggested_price))

    # Fetch live price; reject if Alpaca is unavailable.
    live_price = await asyncio.to_thread(fetch_live_price, rec.symbol)
    if live_price is None:
        raise HTTPException(status_code=503, detail=f"Cannot fetch live price for {rec.symbol}")

    # Drift check: stored recommendation price vs live price.
    drift_pct = abs(live_price - stored_price) / stored_price * 100
    if drift_pct > PRICE_DRIFT_TOLERANCE_PCT:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Price drift {drift_pct:.1f}% exceeds {PRICE_DRIFT_TOLERANCE_PCT}% tolerance — "
                f"stored ${stored_price}, live ${live_price}"
            ),
        )

    # Compute final limit price using strategy buffer.
    strategy = _load_strategy()
    exec_cfg = strategy.get("execution", {})
    if rec.action == "buy":
        buf = Decimal(str(exec_cfg.get("buy_limit_buffer_pct", 0.3))) / Decimal("100")
        limit_price = (live_price * (Decimal("1") + buf)).quantize(Decimal("0.01"))
    else:
        buf = Decimal(str(exec_cfg.get("sell_limit_buffer_pct", 0.3))) / Decimal("100")
        limit_price = (live_price * (Decimal("1") - buf)).quantize(Decimal("0.01"))

    # Re-run all buy-side gates immediately before broker submission.
    if rec.action == "buy":
        portfolio = (await db.execute(select(Portfolio))).scalars().first()
        held = list((await db.execute(select(Position))).scalars().all())
        held_symbols = {p.symbol.upper() for p in held}

        # Compute total portfolio value using live prices (best-effort).
        from ..services.recommender import _compute_portfolio_total_value
        price_data = {rec.symbol: {"current_price": float(live_price)}}
        total_value = _compute_portfolio_total_value(
            Decimal(str(portfolio.cash_balance)),
            held,
            price_data,
        )

        existing_pos = next((p for p in held if p.symbol.upper() == rec.symbol.upper()), None)
        existing_value = (Decimal(str(existing_pos.shares)) * live_price) if existing_pos else Decimal("0")

        # Sector lookups are sync and may call Finnhub — wrap in threads.
        from ..services.recommender import _get_sector_for_symbol
        proposed_sector = await asyncio.to_thread(_get_sector_for_symbol, rec.symbol)
        held_sectors = await asyncio.gather(
            *(asyncio.to_thread(_get_sector_for_symbol, p.symbol) for p in held)
        )
        held_with_sector = [
            {
                "symbol": p.symbol,
                "sector": held_sectors[i],
                "market_value": Decimal(str(p.shares)) * live_price,
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
                rec.symbol, body.recommendation_id, gate_result.reason,
            )
            raise HTTPException(
                status_code=422,
                detail=f"Risk gate rejected at confirm time: {gate_result.reason}",
            )

    broker = get_broker(db)
    try:
        if rec.action == "buy":
            result = await broker.submit_buy(
                symbol=rec.symbol,
                qty=qty,
                limit_price=limit_price,
                recommendation_id=body.recommendation_id,
            )
        else:
            result = await broker.submit_sell(
                symbol=rec.symbol,
                qty=qty,
                limit_price=limit_price,
                recommendation_id=body.recommendation_id,
            )
    except Exception as exc:
        logger.error(
            "Broker order failed: symbol=%s action=%s recommendation_id=%s error=%s",
            rec.symbol, rec.action, body.recommendation_id, exc,
            exc_info=True,
        )
        await send_telegram(
            f"🚨 BROKER ERROR — order may have filled on Alpaca!\n"
            f"Symbol: {rec.symbol}\n"
            f"Action: {rec.action}\n"
            f"Shares: {qty}\n"
            f"Recommendation ID: {body.recommendation_id}\n"
            f"Error: {exc}"
        )
        raise HTTPException(
            status_code=500,
            detail=(
                f"Broker call failed for {rec.action} {rec.symbol} "
                f"(rec_id={body.recommendation_id}). "
                f"The order may have filled on Alpaca — check broker dashboard. "
                f"Error: {exc}"
            ),
        )

    if result["status"] == "submitted":
        # Alpaca fire-and-forget: order submitted, reconcile later.
        rec.status = "submitted"
        await db.commit()
        return ConfirmTradeResponse(
            trade_id=0,
            symbol=rec.symbol,
            action=rec.action,
            shares=result["filled_qty"],
            execution_price=result["filled_avg_price"],
            total_value=Decimal("0"),
            new_cash_balance=Decimal("0"),
            position=None,
            realized_gain=None,
            tax_category=None,
        )

    if result["status"] != "filled":
        raise HTTPException(
            status_code=422,
            detail=f"Order not filled: status={result['status']} for {rec.symbol}"
        )

    pos = (await db.execute(
        select(Position).where(Position.symbol == rec.symbol)
    )).scalars().first()

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


@router.post("/reconcile", dependencies=[Depends(require_owner_pin)])
async def reconcile_orders(db: AsyncSession = Depends(get_db)):
    """Check pending Alpaca orders for fills and record them in the local DB.

    Call ~15 min after Phase 2 to catch orders that filled after submission.
    Safe to call multiple times — already-reconciled orders are skipped.
    """
    from ..broker.alpaca import reconcile_pending_orders
    from ..services.telegram import send_telegram

    results = await reconcile_pending_orders(db)

    # Build Telegram summary
    if results:
        filled = [r for r in results if r["status"] == "filled"]
        unfilled = [r for r in results if r["status"] != "filled"]
        msg_parts = []
        if filled:
            msg_parts.append("Fills recorded:\n" + "\n".join(
                f"  {r['action'].upper()} {r['symbol']} — {r['filled_qty']}sh @ ${r['filled_price']}"
                for r in filled
            ))
        if unfilled:
            msg_parts.append("Not filled:\n" + "\n".join(
                f"  {r['action'].upper()} {r['symbol']} — {r['status']}"
                for r in unfilled
            ))
        await send_telegram("TRADEBOT // Order Reconciliation\n" + "\n".join(msg_parts))

    return {"reconciled": len(results), "results": results}


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
