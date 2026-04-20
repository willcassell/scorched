"""Market data REST endpoints — opening prices and end-of-day summary."""
from datetime import date as date_cls
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..tz import market_today
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..services.research import fetch_market_eod, fetch_opening_prices
from .deps import require_owner_pin

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/opening-prices")
async def opening_prices(
    symbols: str = Query(..., description="Comma-separated ticker symbols, e.g. AAPL,NVDA"),
    date: Optional[str] = Query(None, description="ISO date (YYYY-MM-DD). Defaults to today."),
):
    """Fetch actual opening auction prices for a list of symbols on a given date."""
    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    trade_date = date_cls.fromisoformat(date) if date else market_today()
    prices = await fetch_opening_prices(symbol_list, trade_date)
    return {"date": trade_date.isoformat(), "opening_prices": prices}


@router.get("/current-prices")
async def current_prices(
    symbols: str = Query(..., description="Comma-separated ticker symbols, e.g. AAPL,NVDA"),
):
    """Live snapshot prices (latest trade) for a list of symbols.

    Used by Phase 2 to price sell limits off the current quote instead of the
    9:30 open — stocks that open at the high and sell off otherwise leave their
    limits unreachable for the rest of the session.
    """
    from ..services.alpaca_data import alpaca_snapshots
    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    snaps = await alpaca_snapshots(symbol_list)
    prices = {
        sym: round(float(data["current_price"]), 2)
        for sym, data in snaps.items()
        if data.get("current_price")
    }
    return {"current_prices": prices}


@router.get("/eod-summary")
async def eod_summary(
    date: Optional[str] = Query(None, description="ISO date (YYYY-MM-DD). Defaults to today."),
):
    """Fetch end-of-day performance for major indices and all S&P 500 sector ETFs."""
    target_date = date_cls.fromisoformat(date) if date else market_today()
    result = await fetch_market_eod(target_date)
    return {"date": target_date.isoformat(), **result}


@router.post("/eod-review", dependencies=[Depends(require_owner_pin)])
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
    review_date = date_cls.fromisoformat(date) if date else market_today()
    return await run_eod_review(db, review_date)


@router.post("/weekly-reflection", dependencies=[Depends(require_owner_pin)])
async def weekly_reflection(db: AsyncSession = Depends(get_db)):
    """Weekly trade reflection — reviews past week's trades for learnings.
    Run Sunday evening to prepare for the next trading week.
    """
    from ..services.reflection import generate_weekly_reflection
    return await generate_weekly_reflection(db)
