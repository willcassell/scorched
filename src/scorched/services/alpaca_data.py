"""Alpaca Data API — price bars, snapshots, news, and screener.

Replaces yfinance for price data (more reliable, no scraping) and
Polygon for news (free with Alpaca account, no extra API key).

All SDK calls are synchronous — async wrappers use run_in_executor.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

# Lazy-initialized clients (created on first use)
_data_client = None
_news_client = None
_screener_client = None


def _get_data_client():
    global _data_client
    if _data_client is None:
        from alpaca.data.historical import StockHistoricalDataClient
        _data_client = StockHistoricalDataClient(
            settings.alpaca_api_key, settings.alpaca_secret_key
        )
    return _data_client


def _get_news_client():
    global _news_client
    if _news_client is None:
        from alpaca.data.historical.news import NewsClient
        _news_client = NewsClient(
            settings.alpaca_api_key, settings.alpaca_secret_key
        )
    return _news_client


def _get_screener_client():
    global _screener_client
    if _screener_client is None:
        from alpaca.data.historical.screener import ScreenerClient
        _screener_client = ScreenerClient(
            settings.alpaca_api_key, settings.alpaca_secret_key
        )
    return _screener_client


def _api_ctx(tracker, endpoint, symbol=None):
    if tracker is None:
        from contextlib import nullcontext
        return nullcontext()
    from ..api_tracker import track_call
    return track_call(tracker, "alpaca_data", endpoint, symbol=symbol)


# ── Price Data ──────────────────────────────────────────────────────────────


def fetch_snapshots_sync(
    symbols: list[str], tracker=None
) -> dict[str, dict[str, Any]]:
    """Fetch latest snapshots for multiple symbols.

    Returns {symbol: {"current_price", "prev_close", "daily_bar", ...}}
    """
    from alpaca.data.requests import StockSnapshotRequest
    from alpaca.data.enums import DataFeed

    client = _get_data_client()
    result = {}

    # Alpaca snapshots support batch (up to ~200 symbols per call)
    # Use IEX feed (free tier) — SIP requires paid subscription
    batch_size = 100
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            with _api_ctx(tracker, "snapshots", f"batch_{i}"):
                snaps = client.get_stock_snapshot(
                    StockSnapshotRequest(symbol_or_symbols=batch, feed=DataFeed.IEX)
                )
            for sym, snap in snaps.items():
                try:
                    daily = snap.daily_bar
                    prev = snap.previous_daily_bar
                    result[sym] = {
                        "current_price": float(snap.latest_trade.price),
                        "prev_close": float(prev.close) if prev else None,
                        "daily_open": float(daily.open) if daily else None,
                        "daily_high": float(daily.high) if daily else None,
                        "daily_low": float(daily.low) if daily else None,
                        "daily_close": float(daily.close) if daily else None,
                        "daily_volume": float(daily.volume) if daily else 0,
                        "latest_trade_ts": snap.latest_trade.timestamp.isoformat(),
                    }
                except Exception:
                    logger.debug("Snapshot parse failed for %s", sym, exc_info=True)
        except Exception:
            logger.warning("Snapshot batch %d failed", i, exc_info=True)

    return result


def fetch_bars_sync(
    symbols: list[str],
    days: int = 252,
    tracker=None,
) -> dict[str, list[dict]]:
    """Fetch daily bars for multiple symbols.

    Returns {symbol: [{"date", "open", "high", "low", "close", "volume"}, ...]}
    Sorted oldest to newest.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed

    client = _get_data_client()
    result = {}
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days + 10)  # extra buffer for weekends/holidays

    def _fetch_batch(batch_symbols: list[str], batch_label: str) -> dict:
        """Fetch one batch; return {symbol: [bar dicts, ...]} or empty on failure."""
        batch_result = {}
        try:
            with _api_ctx(tracker, "bars", batch_label):
                bars = client.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=batch_symbols,
                    timeframe=TimeFrame.Day,
                    start=start,
                    end=end,
                    feed=DataFeed.IEX,
                ))
            bars_dict = bars.data if hasattr(bars, 'data') else bars.dict()
            for sym in batch_symbols:
                sym_bars = bars_dict.get(sym, [])
                batch_result[sym] = [
                    {
                        "date": b.timestamp.date().isoformat(),
                        "open": float(b.open),
                        "high": float(b.high),
                        "low": float(b.low),
                        "close": float(b.close),
                        "volume": float(b.volume),
                    }
                    for b in sym_bars
                ]
        except Exception as exc:
            # "invalid symbol" from Alpaca fails the entire batch. Recover by
            # splitting in half (down to 1) so one bad ticker only loses itself.
            if len(batch_symbols) > 1:
                msg = str(exc)
                if "invalid symbol" in msg.lower() or "bad request" in msg.lower() or "422" in msg:
                    logger.info(
                        "Bars batch %s failed (%s) — bisecting to isolate bad symbol",
                        batch_label, msg[:120],
                    )
                    mid = len(batch_symbols) // 2
                    batch_result.update(_fetch_batch(batch_symbols[:mid], f"{batch_label}a"))
                    batch_result.update(_fetch_batch(batch_symbols[mid:], f"{batch_label}b"))
                    return batch_result
            logger.warning("Bars batch %s failed", batch_label, exc_info=True)
        return batch_result

    # Alpaca supports multi-symbol bar requests
    # Use IEX feed (free tier) — SIP requires paid subscription
    batch_size = 50
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        result.update(_fetch_batch(batch, f"batch_{i}"))

    return result


def fetch_latest_bars_sync(
    symbols: list[str], tracker=None
) -> dict[str, dict]:
    """Fetch the latest daily bar for multiple symbols."""
    from alpaca.data.requests import StockLatestBarRequest
    from alpaca.data.enums import DataFeed

    client = _get_data_client()
    try:
        with _api_ctx(tracker, "latest_bars"):
            latest = client.get_stock_latest_bar(
                StockLatestBarRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX)
            )
        return {
            sym: {
                "close": float(bar.close),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "volume": float(bar.volume),
                "timestamp": bar.timestamp.isoformat(),
            }
            for sym, bar in latest.items()
        }
    except Exception:
        logger.warning("Latest bars fetch failed", exc_info=True)
        return {}


# ── News ────────────────────────────────────────────────────────────────────


def fetch_news_sync(
    symbols: list[str], limit_per_symbol: int = 5, tracker=None
) -> dict[str, list[dict]]:
    """Fetch news articles from Alpaca for each symbol.

    Returns {symbol: [{"headline", "summary", "source", "created_at"}, ...]}
    """
    from alpaca.data.requests import NewsRequest

    client = _get_news_client()
    result = {}

    for symbol in symbols:
        try:
            with _api_ctx(tracker, "news", symbol):
                news_set = client.get_news(NewsRequest(
                    symbols=symbol,
                    limit=limit_per_symbol,
                ))
            articles = []
            news_data = news_set.dict().get("news", [])
            for article in news_data:
                headline = article.get("headline", "")
                summary = article.get("summary", "")
                if headline:
                    articles.append({
                        "headline": headline,
                        "summary": summary,
                        "source": article.get("source", ""),
                        "created_at": str(article.get("created_at", "")),
                        "symbols": article.get("symbols", []),
                    })
            result[symbol] = articles
        except Exception:
            logger.warning("Alpaca news fetch failed for %s", symbol, exc_info=True)
            result[symbol] = []
        # Rate limit: be gentle
        time.sleep(0.3)

    return result


# ── Screener ────────────────────────────────────────────────────────────────


def fetch_most_actives_sync(top: int = 20, tracker=None) -> list[dict]:
    """Fetch most active stocks by volume."""
    from alpaca.data.requests import MostActivesRequest

    client = _get_screener_client()
    try:
        with _api_ctx(tracker, "most_actives"):
            result = client.get_most_actives(MostActivesRequest(top=top))
        return [
            {"symbol": s.symbol, "volume": s.volume, "trade_count": s.trade_count}
            for s in result.most_actives
        ]
    except Exception:
        logger.warning("Most actives fetch failed", exc_info=True)
        return []


def fetch_market_movers_sync(top: int = 10, tracker=None) -> dict:
    """Fetch top gainers and losers."""
    from alpaca.data.requests import MarketMoversRequest

    client = _get_screener_client()
    try:
        with _api_ctx(tracker, "market_movers"):
            result = client.get_market_movers(MarketMoversRequest(top=top))
        return {
            "gainers": [
                {"symbol": s.symbol, "change": s.change, "percent_change": s.percent_change}
                for s in result.gainers
            ],
            "losers": [
                {"symbol": s.symbol, "change": s.change, "percent_change": s.percent_change}
                for s in result.losers
            ],
        }
    except Exception:
        logger.warning("Market movers fetch failed", exc_info=True)
        return {"gainers": [], "losers": []}


# ── Async wrappers ──────────────────────────────────────────────────────────


async def alpaca_snapshots(symbols: list[str], tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: fetch_snapshots_sync(symbols, tracker=tracker)
    )


async def alpaca_bars(symbols: list[str], days: int = 252, tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: fetch_bars_sync(symbols, days=days, tracker=tracker)
    )


async def alpaca_latest_bars(symbols: list[str], tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: fetch_latest_bars_sync(symbols, tracker=tracker)
    )


async def alpaca_news(symbols: list[str], limit_per_symbol: int = 5, tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: fetch_news_sync(symbols, limit_per_symbol, tracker=tracker)
    )


async def alpaca_most_actives(top: int = 20, tracker=None) -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: fetch_most_actives_sync(top, tracker=tracker)
    )


async def alpaca_market_movers(top: int = 10, tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: fetch_market_movers_sync(top, tracker=tracker)
    )
