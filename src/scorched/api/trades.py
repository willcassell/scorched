from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import TradeRecommendation, Position
from ..schemas import ConfirmTradeRequest, ConfirmTradeResponse, PositionDetail, RejectTradeRequest, RejectTradeResponse
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
