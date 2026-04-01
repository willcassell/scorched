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
from ..services.finnhub_data import fetch_analyst_consensus_sync, build_analyst_context
from ..services.research import (
    WATCHLIST,
    build_research_context,
    fetch_av_technicals,
    fetch_earnings_surprise,
    fetch_edgar_insider,
    fetch_fred_macro,
    fetch_market_context,
    fetch_momentum_screener,
    fetch_news,
    fetch_polygon_news,
    fetch_price_data,
)
from ..services.technicals import compute_technicals

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research", tags=["research"])

CACHE_DIR = "/tmp"


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


@router.post("/prefetch")
async def prefetch_research(db: AsyncSession = Depends(get_db)):
    """Fetch all external research data and cache processed results.

    Called by Phase 0 cron at 7:30 AM ET. The cache is consumed by
    Phase 1 (generate_recommendations) at 8:30 AM ET.
    """
    import asyncio
    from datetime import date as date_type, datetime

    session_date = date_type.today()
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

    # 2. Parallel data fetch
    with _timed("parallel_fetch", timing):
        (
            price_data, news_data, earnings_surprise, insider_activity,
            market_context, fred_macro, polygon_news, av_technicals
        ) = await asyncio.gather(
            fetch_price_data(research_symbols, tracker=tracker),
            fetch_news(research_symbols, tracker=tracker),
            fetch_earnings_surprise(research_symbols, tracker=tracker),
            fetch_edgar_insider(research_symbols, tracker=tracker),
            fetch_market_context(session_date, research_symbols, tracker=tracker),
            fetch_fred_macro(settings.fred_api_key, tracker=tracker),
            fetch_polygon_news(research_symbols, settings.polygon_api_key, tracker=tracker),
            fetch_av_technicals(screener_symbols, settings.alpha_vantage_api_key, tracker=tracker),
        )

    # 3. Technicals (pure math, fast)
    with _timed("technicals", timing):
        technicals = compute_technicals(price_data)
    logger.info("Phase 0: computed technicals for %d symbols", len(technicals))

    # 4. Finnhub analyst consensus (sequential, rate-limited)
    finnhub_client = None
    if settings.finnhub_api_key:
        import finnhub
        finnhub_client = finnhub.Client(api_key=settings.finnhub_api_key)

    with _timed("finnhub", timing):
        analyst_consensus = await asyncio.get_event_loop().run_in_executor(
            None, lambda: fetch_analyst_consensus_sync(research_symbols, finnhub_client, tracker=tracker)
        )
    logger.info("Phase 0: fetched analyst consensus for %d symbols", len(analyst_consensus))

    # 5. Build the analyst context text
    analyst_context = build_analyst_context(analyst_consensus)

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
        "created_at": datetime.utcnow().isoformat() + "Z",
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
        "technicals": technicals,
        "analyst_consensus": analyst_consensus,
        "analyst_context": analyst_context,
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
