"""Phase 0 — prefetch all external research data and cache for Phase 1."""
import json
import logging
import os
import tempfile
import time

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api_tracker import ApiCallTracker
from ..config import settings
from ..database import get_db
from ..models import Position
from .deps import require_owner_pin
from ..services.economic_calendar import fetch_economic_calendar, build_economic_calendar_context
from ..services.finnhub_data import fetch_analyst_consensus_sync, build_analyst_context, fetch_congressional_trading_sync, build_congressional_context
from ..services.research import (
    WATCHLIST,
    build_research_context,
    compute_relative_strength,
    fetch_av_technicals,
    fetch_twelvedata_rsi,
    fetch_earnings_surprise,
    fetch_edgar_insider,
    fetch_fred_macro,
    fetch_market_context,
    fetch_momentum_screener,
    fetch_news,
    fetch_polygon_news,
    fetch_premarket_prices,
    fetch_price_data,
    fetch_sector_returns,
)
from ..services.technicals import compute_technicals

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research", tags=["research"])

CACHE_DIR = "/app/logs"


def cache_path_for_date(d: str) -> str:
    return os.path.join(CACHE_DIR, f"tradebot_research_cache_{d}.json")


def _timed(name: str, timing: dict):
    """Context-manager-style timer that logs and records elapsed time."""
    class _Timer:
        def __init__(self):
            self.start = None
        def __enter__(self):
            self.start = time.monotonic()
            return self
        def __exit__(self, *exc):
            elapsed = time.monotonic() - self.start
            timing[name] = round(elapsed, 1)
            logger.info("Phase 0: %s completed in %.1fs", name, elapsed)
    return _Timer()


@router.post("/prefetch", dependencies=[Depends(require_owner_pin)])
async def prefetch_research(db: AsyncSession = Depends(get_db)):
    """Fetch all external research data and cache processed results.

    Called by Phase 0 cron at 7:30 AM ET. The cache is consumed by
    Phase 1 (generate_recommendations) at 8:30 AM ET.
    """
    import asyncio
    from datetime import date as date_type, datetime, timezone

    from ..tz import market_today
    session_date = market_today()
    date_str = session_date.isoformat()
    timing = {}
    total_start = time.monotonic()

    tracker = ApiCallTracker()

    # Current positions (needed for research symbol list)
    current_positions = (await db.execute(select(Position))).scalars().all()
    current_symbols = [p.symbol for p in current_positions]

    # 1. Momentum screener — scans all SP500, returns top 20
    with _timed("momentum_screener", timing):
        screener_symbols = await fetch_momentum_screener(n=20, tracker=tracker)
    logger.info("Phase 0: screener returned %d symbols: %s", len(screener_symbols), screener_symbols)

    research_symbols = list(set(WATCHLIST + current_symbols + screener_symbols))
    logger.info("Phase 0: research universe = %d symbols", len(research_symbols))

    # 2. Parallel data fetch — each source timed individually
    async def _timed_fetch(name, coro):
        start = time.monotonic()
        result = await coro
        elapsed = time.monotonic() - start
        timing[name] = round(elapsed, 1)
        logger.info("Phase 0: %s completed in %.1fs", name, elapsed)
        return result

    parallel_start = time.monotonic()
    (
        price_data, news_data, earnings_surprise, insider_activity,
        market_context, fred_macro, polygon_news, av_technicals, twelvedata_rsi,
        sector_returns, premarket_data, economic_calendar
    ) = await asyncio.gather(
        _timed_fetch("price_data", fetch_price_data(research_symbols, tracker=tracker)),
        _timed_fetch("news", fetch_news(research_symbols, tracker=tracker)),
        _timed_fetch("earnings_surprise", fetch_earnings_surprise(research_symbols, tracker=tracker)),
        _timed_fetch("edgar_insider", fetch_edgar_insider(research_symbols, tracker=tracker)),
        _timed_fetch("market_context", fetch_market_context(session_date, research_symbols, tracker=tracker)),
        _timed_fetch("fred_macro", fetch_fred_macro(settings.fred_api_key, tracker=tracker)),
        _timed_fetch("polygon_news", fetch_polygon_news(research_symbols, settings.polygon_api_key, tracker=tracker)),
        _timed_fetch("av_technicals", fetch_av_technicals(screener_symbols, settings.alpha_vantage_api_key, tracker=tracker)),
        _timed_fetch("twelvedata_rsi", fetch_twelvedata_rsi(research_symbols, settings.twelvedata_api_key, tracker=tracker)),
        _timed_fetch("sector_returns", fetch_sector_returns(tracker=tracker)),
        _timed_fetch("premarket", fetch_premarket_prices(research_symbols, tracker=tracker)),
        _timed_fetch("economic_calendar", fetch_economic_calendar(settings.fred_api_key, tracker=tracker)),
    )
    timing["parallel_fetch_wall"] = round(time.monotonic() - parallel_start, 1)
    logger.info("Phase 0: parallel_fetch wall time %.1fs", timing["parallel_fetch_wall"])

    # 3. Technicals + relative strength (pure math, fast)
    with _timed("technicals", timing):
        technicals = compute_technicals(price_data)
    logger.info("Phase 0: computed technicals for %d symbols", len(technicals))

    relative_strength = compute_relative_strength(price_data, sector_returns)
    logger.info("Phase 0: computed relative strength for %d symbols", len(relative_strength))

    # 4. Finnhub analyst consensus (sequential, rate-limited)
    finnhub_client = None
    if settings.finnhub_api_key:
        import finnhub
        finnhub_client = finnhub.Client(api_key=settings.finnhub_api_key)

    with _timed("finnhub", timing):
        analyst_consensus = await asyncio.get_running_loop().run_in_executor(
            None, lambda: fetch_analyst_consensus_sync(research_symbols, finnhub_client, tracker=tracker)
        )
    logger.info("Phase 0: fetched analyst consensus for %d symbols", len(analyst_consensus))

    with _timed("finnhub_congress", timing):
        congressional_data = await asyncio.get_running_loop().run_in_executor(
            None, lambda: fetch_congressional_trading_sync(research_symbols, finnhub_client, tracker=tracker)
        )
    logger.info("Phase 0: fetched congressional trading for %d symbols", len(congressional_data))

    # 5. Build the analyst context text
    analyst_context = build_analyst_context(analyst_consensus)
    congressional_context = build_congressional_context(congressional_data)

    # 6. Serialize price_data for cache (convert non-serializable types)
    price_data_cache = {}
    for sym, data in price_data.items():
        entry = {}
        for k, v in data.items():
            if k == "history":
                continue  # skip DataFrame — technicals already computed
            try:
                json.dumps(v)  # test serializable
                entry[k] = v
            except (TypeError, ValueError):
                entry[k] = str(v)
        price_data_cache[sym] = entry

    # Build cache payload
    total_elapsed = time.monotonic() - total_start
    timing["total"] = round(total_elapsed, 1)

    cache = {
        "date": date_str,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "timing": timing,
        "research_symbols": research_symbols,
        "screener_symbols": screener_symbols,
        "current_positions": current_symbols,
        "market_context": market_context,
        "price_data": price_data_cache,
        "news_data": news_data,
        "earnings_surprise": earnings_surprise,
        "insider_activity": insider_activity,
        "fred_macro": fred_macro,
        "polygon_news": polygon_news,
        "av_technicals": av_technicals,
        "twelvedata_rsi": twelvedata_rsi,
        "technicals": technicals,
        "analyst_consensus": analyst_consensus,
        "analyst_context": analyst_context,
        "congressional_data": congressional_data,
        "congressional_context": congressional_context,
        "sector_returns": sector_returns,
        "relative_strength": relative_strength,
        "premarket_data": premarket_data,
        "economic_calendar": economic_calendar,
        "economic_calendar_context": build_economic_calendar_context(economic_calendar),
    }

    # Atomic write
    out_path = cache_path_for_date(date_str)
    fd, tmp_path = tempfile.mkstemp(dir=CACHE_DIR, prefix="tradebot_research_cache_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f)
        os.rename(tmp_path, out_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Flush API call tracker
    await tracker.flush(db)
    await db.commit()

    logger.info("Phase 0: TOTAL completed in %.1fs — cache written to %s", total_elapsed, out_path)

    if total_elapsed > 3300:  # 55 min — cutting into Phase 1 window
        logger.warning("Phase 0: took %.0fs (>55min) — dangerously close to Phase 1 start", total_elapsed)

    return {
        "status": "ok",
        "date": date_str,
        "research_symbols": len(research_symbols),
        "screener_symbols": screener_symbols,
        "timing": timing,
        "cache_path": out_path,
    }


@router.get("/company-names")
async def get_company_names():
    """Return symbol→company name map from the latest Phase 0 cache."""
    from datetime import date as date_type
    from ..tz import market_today
    today = market_today().isoformat()
    path = cache_path_for_date(today)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            cache = json.load(f)
        price_data = cache.get("price_data", {})
        return {sym: d.get("company_name", "") for sym, d in price_data.items() if d.get("company_name")}
    except Exception:
        return {}
