from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import TradeHistory
from ..schemas import BenchmarkResponse, PortfolioResponse, TaxSummaryResponse, TradeHistoryItem
from ..services import portfolio as portfolio_svc

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
