import logging
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import TradeRecommendation, Position
from ..schemas import ConfirmTradeRequest, ConfirmTradeResponse, PositionDetail, RejectTradeRequest, RejectTradeResponse
from ..broker import get_broker
from ..services.telegram import send_telegram
from .deps import require_owner_pin

logger = logging.getLogger(__name__)

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
    if rec.status not in ("pending", "submitted"):
        raise HTTPException(status_code=409, detail=f"Recommendation is already {rec.status}")

    broker = get_broker(db)

    try:
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
    except Exception as exc:
        logger.error(
            "Broker order failed: symbol=%s action=%s recommendation_id=%s error=%s",
            rec.symbol, rec.action, body.recommendation_id, exc,
            exc_info=True,
        )
        alert = (
            f"🚨 BROKER ERROR — order may have filled on Alpaca!\n"
            f"Symbol: {rec.symbol}\n"
            f"Action: {rec.action}\n"
            f"Shares: {body.shares}\n"
            f"Recommendation ID: {body.recommendation_id}\n"
            f"Error: {exc}"
        )
        await send_telegram(alert)
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
        # Alpaca fire-and-forget: order submitted, reconcile later
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

    # Build response compatible with existing ConfirmTradeResponse schema
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
