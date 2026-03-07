from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import TradeRecommendation
from ..schemas import ConfirmTradeRequest, ConfirmTradeResponse, RejectTradeRequest, RejectTradeResponse
from ..services import portfolio as portfolio_svc
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

    if rec.action == "buy":
        return await portfolio_svc.apply_buy(
            db,
            recommendation_id=body.recommendation_id,
            symbol=rec.symbol,
            shares=body.shares,
            execution_price=body.execution_price,
            executed_at=datetime.utcnow(),
        )
    else:
        return await portfolio_svc.apply_sell(
            db,
            recommendation_id=body.recommendation_id,
            symbol=rec.symbol,
            shares=body.shares,
            execution_price=body.execution_price,
            executed_at=datetime.utcnow(),
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
