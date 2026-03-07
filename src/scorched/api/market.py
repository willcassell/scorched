"""Market data REST endpoints — opening prices and end-of-day summary."""
from datetime import date as date_cls
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..services.research import fetch_market_eod, fetch_opening_prices

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/opening-prices")
async def opening_prices(
    symbols: str = Query(..., description="Comma-separated ticker symbols, e.g. AAPL,NVDA"),
    date: Optional[str] = Query(None, description="ISO date (YYYY-MM-DD). Defaults to today."),
):
    """Fetch actual opening auction prices for a list of symbols on a given date."""
    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    trade_date = date_cls.fromisoformat(date) if date else date_cls.today()
    prices = await fetch_opening_prices(symbol_list, trade_date)
    return {"date": trade_date.isoformat(), "opening_prices": prices}


@router.get("/eod-summary")
async def eod_summary(
    date: Optional[str] = Query(None, description="ISO date (YYYY-MM-DD). Defaults to today."),
):
    """Fetch end-of-day performance for major indices and all S&P 500 sector ETFs."""
    target_date = date_cls.fromisoformat(date) if date else date_cls.today()
    result = await fetch_market_eod(target_date)
    return {"date": target_date.isoformat(), **result}


@router.post("/eod-review")
async def eod_review(
    date: Optional[str] = Query(None, description="ISO date (YYYY-MM-DD). Defaults to today."),
    db: AsyncSession = Depends(get_db),
):
    """
    End-of-day performance review. Compares this morning's recommendations against
    actual intraday moves, calls Claude to extract learnings, and updates the playbook.
    Run this after market close (~4:05 PM ET).
    """
    from ..services.eod_review import run_eod_review
    review_date = date_cls.fromisoformat(date) if date else date_cls.today()
    return await run_eod_review(db, review_date)
