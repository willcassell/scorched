import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import Position, TradeHistory
from ..schemas import (
    BenchmarkResponse,
    PortfolioResponse,
    PortfolioRiskResponse,
    TaxSummaryResponse,
    TradeHistoryItem,
)
from ..services import portfolio as portfolio_svc
from ..services import risk as risk_svc
from .deps import require_owner_pin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("", response_model=PortfolioResponse)
async def get_portfolio(db: AsyncSession = Depends(get_db)):
    return await portfolio_svc.get_portfolio_state(db)


@router.get("/history", response_model=list[TradeHistoryItem])
async def get_trade_history(
    symbol: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    q = select(TradeHistory).order_by(TradeHistory.executed_at.desc()).limit(limit).offset(offset)
    if symbol:
        q = q.where(TradeHistory.symbol == symbol.upper())
    rows = (await db.execute(q)).scalars().all()
    return rows


@router.get("/benchmarks", response_model=BenchmarkResponse)
async def get_benchmarks(db: AsyncSession = Depends(get_db)):
    return await portfolio_svc.get_benchmark_comparison(db)


@router.get("/tax-summary", response_model=TaxSummaryResponse)
async def get_tax_summary(db: AsyncSession = Depends(get_db)):
    return await portfolio_svc.get_tax_summary(db)


@router.get("/risk", response_model=PortfolioRiskResponse)
async def get_portfolio_risk(
    confidence: float = Query(0.95, ge=0.50, le=0.999),
    lookback_days: int = Query(252, ge=30, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """1-day historical-simulation VaR & CVaR for the current book.

    `var_pct` is a negative number (loss); the dashboard should render |var_pct|
    as the "1-day worst-case at <confidence>" line and `cvar_dollars` as the
    "if it gets that bad, expect this much" line.
    """
    result = await risk_svc.compute_portfolio_risk(
        db, confidence=confidence, lookback_days=lookback_days
    )
    return PortfolioRiskResponse(
        confidence=result.confidence,
        lookback_days=result.lookback_days,
        n_positions=result.n_positions,
        portfolio_value=result.portfolio_value,
        var_pct=result.var_pct,
        cvar_pct=result.cvar_pct,
        var_dollars=result.var_dollars,
        cvar_dollars=result.cvar_dollars,
    )


class TrailingStopUpdate(BaseModel):
    high_water_mark: float
    trailing_stop_price: float


class TrailingStopUpdateResponse(BaseModel):
    symbol: str
    high_water_mark: float
    trailing_stop_price: float
    updated: bool


@router.post(
    "/positions/{symbol}/trailing-stop",
    response_model=TrailingStopUpdateResponse,
    dependencies=[Depends(require_owner_pin)],
)
async def update_position_trailing_stop(
    symbol: str,
    body: TrailingStopUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update the trailing stop and high-water mark for a held position.

    Only raises the high_water_mark (ratchet — will not lower it).
    The trailing_stop_price is similarly ratcheted up only.
    Called by the intraday monitor on each 5-minute tick.
    """
    symbol = symbol.upper()
    pos = (await db.execute(select(Position).where(Position.symbol == symbol))).scalars().first()
    if pos is None:
        raise HTTPException(status_code=404, detail=f"Position {symbol} not found")

    new_hwm = Decimal(str(body.high_water_mark)).quantize(Decimal("0.0001"))
    new_stop = Decimal(str(body.trailing_stop_price)).quantize(Decimal("0.0001"))

    # Ratchet: only update if the new value is strictly higher than the stored value
    hwm_updated = False
    if pos.high_water_mark is None or new_hwm > pos.high_water_mark:
        pos.high_water_mark = new_hwm
        hwm_updated = True

    stop_updated = False
    if pos.trailing_stop_price is None or new_stop > pos.trailing_stop_price:
        pos.trailing_stop_price = new_stop
        stop_updated = True

    if hwm_updated or stop_updated:
        await db.commit()
        await db.refresh(pos)
        logger.info(
            "Trailing stop updated for %s: HWM=%s stop=%s",
            symbol, pos.high_water_mark, pos.trailing_stop_price,
        )

    return TrailingStopUpdateResponse(
        symbol=symbol,
        high_water_mark=float(pos.high_water_mark),
        trailing_stop_price=float(pos.trailing_stop_price),
        updated=hwm_updated or stop_updated,
    )
