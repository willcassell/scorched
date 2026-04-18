"""Stock research: price data, fundamentals, news, macro, and market context via yfinance + FRED."""
import asyncio
import logging
from contextlib import nullcontext
from datetime import date, timedelta
from decimal import Decimal

import yfinance as yf

from ..config import settings
from ..http_retry import retry_call, retry_get
from ..tz import market_today

logger = logging.getLogger(__name__)


def _api_ctx(tracker, service, endpoint, symbol=None):
    """Return a track_call context if tracker is provided, else nullcontext."""
    if tracker is None:
        return nullcontext()
    from ..api_tracker import track_call
    return track_call(tracker, service, endpoint, symbol=symbol)

# Universe of large-cap, liquid stocks to consider each morning
WATCHLIST = [
    # Mega-cap tech (10)
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "ORCL", "CRM",
    # Tech growth (8)
    "AMD", "NFLX", "ADBE", "NOW", "PANW", "CRWD", "DDOG", "PLTR",
    # Financials (8)
    "JPM", "V", "MA", "GS", "BLK", "SPGI", "SCHW", "ICE",
    # Healthcare (6)
    "UNH", "JNJ", "ABBV", "LLY", "MRK", "PFE",
    # Consumer (6)
    "WMT", "HD", "COST", "PG", "KO", "PEP",
    # Energy (6)
    "XOM", "CVX", "COP", "SLB", "HAL", "OXY",
    # Industrials (6)
    "BA", "CAT", "HON", "GE", "RTX", "DE",
    # Other high-liquidity (8)
    "UBER", "DIS", "HOOD", "PYPL", "COIN", "NET", "SNOW", "SHOP",
]


def _fetch_price_data_sync(symbols: list[str], tracker=None) -> dict:
    """Fetch price history from Alpaca (reliable) + fundamentals from yfinance.

    Alpaca provides: daily bars (1y), current price via snapshots.
    yfinance provides: fundamentals (PE, market cap, short ratio, company name).
    """
    from .alpaca_data import fetch_bars_sync, fetch_snapshots_sync

    result = {}

    # Batch fetch from Alpaca — much faster than per-symbol yfinance
    alpaca_bars = fetch_bars_sync(symbols, days=365, tracker=tracker)
    alpaca_snaps = fetch_snapshots_sync(symbols, tracker=tracker)

    for symbol in symbols:
        try:
            bars = alpaca_bars.get(symbol, [])
            snap = alpaca_snaps.get(symbol)
            if not bars:
                logger.warning("No Alpaca bars for %s — skipping", symbol)
                continue

            closes = [b["close"] for b in bars]
            current_price = snap["current_price"] if snap else closes[-1]
            week_ago_price = closes[-5] if len(closes) >= 5 else current_price
            month_ago_price = closes[-22] if len(closes) >= 22 else closes[0]
            high_52w = max(b["high"] for b in bars)
            low_52w = min(b["low"] for b in bars)

            # Fundamentals from yfinance (PE, market cap, etc.) — Alpaca doesn't have these
            info = {}
            try:
                ticker = yf.Ticker(symbol)
                with _api_ctx(tracker, "yfinance", "info", symbol):
                    info = ticker.info
            except Exception:
                logger.debug("yfinance info fetch failed for %s (non-fatal)", symbol)

            result[symbol] = {
                "current_price": current_price,
                "week_change_pct": round((current_price - week_ago_price) / week_ago_price * 100, 2),
                "month_change_pct": round((current_price - month_ago_price) / month_ago_price * 100, 2),
                "high_52w": high_52w,
                "low_52w": low_52w,
                "market_cap": info.get("marketCap"),
                "pe_ratio": info.get("trailingPE"),
                "forward_pe": info.get("forwardPE"),
                "eps_ttm": info.get("trailingEps"),
                "short_ratio": info.get("shortRatio"),
                "short_percent_float": info.get("shortPercentOfFloat"),
                "company_name": info.get("shortName", ""),
                "insider_buy_pct": None,  # populated separately
                "history_close": closes,
                "history_volume": [b["volume"] for b in bars],
                "history_high": [b["high"] for b in bars],
                "history_low": [b["low"] for b in bars],
            }
        except Exception:
            logger.warning("Price data fetch failed for %s", symbol, exc_info=True)
    return result


def _fetch_news_sync(symbols: list[str], tracker=None) -> dict:
    result = {}
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            with _api_ctx(tracker, "yfinance", "news", symbol):
                news = ticker.news or []
            headlines = []
            for item in news[:5]:
                title = item.get("content", {}).get("title", "")
                if title:
                    headlines.append(title)
            result[symbol] = headlines
        except Exception:
            logger.warning("News fetch failed for %s", symbol, exc_info=True)
            result[symbol] = []
    return result


def _fetch_earnings_surprise_sync(symbols: list[str], tracker=None) -> dict:
    """Return last 4 quarters of EPS beat/miss for each symbol."""
    result = {}
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            with _api_ctx(tracker, "yfinance", "earnings", symbol):
                hist = ticker.earnings_history
            if hist is None or (hasattr(hist, "empty") and hist.empty):
                result[symbol] = []
                continue
            rows = []
            for _, row in hist.tail(4).iterrows():
                eps_est = row.get("epsEstimate")
                eps_act = row.get("epsActual")
                if eps_est is not None and eps_act is not None and eps_est != 0:
                    surprise_pct = round((float(eps_act) - float(eps_est)) / abs(float(eps_est)) * 100, 1)
                    verdict = "beat" if surprise_pct > 2 else ("miss" if surprise_pct < -2 else "inline")
                    rows.append({"surprise_pct": surprise_pct, "verdict": verdict})
            result[symbol] = rows
        except Exception:
            logger.warning("Earnings surprise fetch failed for %s", symbol, exc_info=True)
            result[symbol] = []
    return result


def _fetch_insider_activity_sync(symbols: list[str]) -> dict:
    """Return a summary of insider buying activity for each symbol (last 30 days)."""
    result = {}
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            purchases = ticker.insider_purchases
            if purchases is None or (hasattr(purchases, "empty") and purchases.empty):
                result[symbol] = {"recent_buys": 0, "recent_sells": 0}
                continue
            buys = 0
            sells = 0
            for _, row in purchases.iterrows():
                shares = row.get("Shares", 0) or 0
                text = str(row.get("Transaction", "")).lower()
                if "purchase" in text or "buy" in text:
                    buys += int(shares)
                elif "sale" in text or "sell" in text:
                    sells += int(shares)
            result[symbol] = {"recent_buys": buys, "recent_sells": sells}
        except Exception:
            logger.warning("Insider activity fetch failed for %s", symbol, exc_info=True)
            result[symbol] = {"recent_buys": 0, "recent_sells": 0}
    return result


def _build_ticker_to_cik_map(headers: dict) -> dict[str, str]:
    """
    Download the SEC company tickers JSON and return a {TICKER: zero-padded CIK} map.
    This is a single ~2MB request that covers all SEC-registered companies.
    """
    try:
        resp = retry_get(
            "https://www.sec.gov/files/company_tickers.json",
            label="SEC CIK map",
            headers=headers,
            timeout=15,
        )
        data = resp.json()
        return {
            entry["ticker"].upper(): str(entry["cik_str"]).zfill(10)
            for entry in data.values()
        }
    except Exception:
        logger.warning("SEC ticker-to-CIK map fetch failed", exc_info=True)
        return {}


def _fetch_edgar_insider_sync(symbols: list[str], days_back: int = 30, tracker=None) -> dict:
    """
    Fetch Form 4 insider filing counts from SEC EDGAR for each symbol.
    Uses the official data.sec.gov submissions API (free, no key required).
    Falls back to yfinance insider_purchases on any per-symbol error.
    """
    import time
    from datetime import datetime, timedelta

    from ..tz import market_now
    cutoff = market_now() - timedelta(days=days_back)
    headers = {"User-Agent": "ScorchedTradebot/1.0 research@tradebot.local",
               "Accept": "application/json"}
    result = {}

    # Build ticker → CIK map (one request, covers all symbols)
    ticker_cik = _build_ticker_to_cik_map(headers)
    if not ticker_cik:
        # If the map fetch fails entirely, fall back to yfinance for all symbols
        return _fetch_insider_activity_sync(symbols)

    for symbol in symbols:
        cik = ticker_cik.get(symbol.upper())
        if not cik:
            result[symbol] = {"recent_buys": 0, "recent_sells": 0}
            continue
        try:
            # Rate limit: SEC requests max 10 req/sec
            time.sleep(0.12)
            url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            with _api_ctx(tracker, "edgar", "submissions", symbol):
                resp = retry_get(url, label=f"EDGAR {symbol}", headers=headers, timeout=10)
                data = resp.json()

            # Count recent Form 4 filings from the "recent" filings list
            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])

            form4_count = 0
            for form, filing_date in zip(forms, dates):
                if form != "4":
                    continue
                try:
                    fd = datetime.strptime(filing_date, "%Y-%m-%d")
                    if fd >= cutoff:
                        form4_count += 1
                except (ValueError, TypeError):
                    continue

            # Form 4 filings indicate insider transactions — the submissions API
            # doesn't break down buy vs sell in its metadata, so we report total
            # filing count and let Claude know the type is unknown.
            result[symbol] = {"form4_filings": form4_count, "recent_buys": 0, "recent_sells": 0}
        except Exception:
            logger.warning("EDGAR insider fetch failed for %s, falling back to yfinance", symbol, exc_info=True)
            # Fallback: yfinance
            try:
                ticker = yf.Ticker(symbol)
                purchases = ticker.insider_purchases
                if purchases is None or (hasattr(purchases, "empty") and purchases.empty):
                    result[symbol] = {"recent_buys": 0, "recent_sells": 0}
                    continue
                buys = sells = 0
                for _, row in purchases.iterrows():
                    shares = int(row.get("Shares", 0) or 0)
                    text = str(row.get("Transaction", "")).lower()
                    if "purchase" in text or "buy" in text:
                        buys += shares
                    elif "sale" in text or "sell" in text:
                        sells += shares
                result[symbol] = {"recent_buys": buys, "recent_sells": sells}
            except Exception:
                logger.warning("Insider fallback (yfinance) also failed for %s", symbol, exc_info=True)
                result[symbol] = {"recent_buys": 0, "recent_sells": 0}
    return result


def _fetch_polygon_news_sync(symbols: list[str], api_key: str, limit_per_symbol: int = 5, tracker=None) -> dict:
    """Fetch news from Alpaca Data API (replaces Polygon).

    Returns {symbol: [{"title": ..., "description": ...}, ...]}
    Maintains the same return format so build_research_context() doesn't change.
    Falls back to empty on any error.
    """
    from .alpaca_data import fetch_news_sync as _alpaca_news

    # Use Alpaca news regardless of Polygon API key — it's free with Alpaca account
    if not settings.alpaca_api_key:
        return {}

    alpaca_result = _alpaca_news(symbols, limit_per_symbol=limit_per_symbol, tracker=tracker)

    # Convert Alpaca format to existing Polygon format for compatibility
    result = {}
    for symbol, articles in alpaca_result.items():
        result[symbol] = [
            {
                "title": a.get("headline", ""),
                "description": a.get("summary", ""),
            }
            for a in articles
            if a.get("headline")
        ]
    return result


def _fetch_av_technicals_sync(symbols: list[str], api_key: str, tracker=None) -> dict:
    """
    Fetch RSI(14) from Alpha Vantage for each symbol.
    Returns {symbol: {"rsi": float, "signal": "overbought"|"oversold"|"neutral"}}
    Should only be called for screener picks (≤20 symbols) to stay within 25 calls/day free tier.
    Includes rate limiting (1.2s between calls) to avoid hitting the 5 calls/min free-tier limit.
    """
    import time
    if not api_key or not symbols:
        return {}
    result = {}
    base = "https://www.alphavantage.co/query"
    for symbol in symbols:
        try:
            with _api_ctx(tracker, "alpha_vantage", "rsi", symbol):
                resp = retry_get(
                    base,
                    label=f"Alpha Vantage {symbol}",
                    params={
                        "function": "RSI",
                        "symbol": symbol,
                        "interval": "daily",
                        "time_period": 14,
                        "series_type": "close",
                        "apikey": api_key,
                    },
                    timeout=15,
                )
                data = resp.json()
            # AV returns an error note when rate limited
            if "Note" in data or "Information" in data:
                break  # stop making calls — daily/minute limit hit
            tech = data.get("Technical Analysis: RSI", {})
            if not tech:
                continue
            latest_date = sorted(tech.keys())[-1]
            rsi = round(float(tech[latest_date]["RSI"]), 1)
            signal = "overbought" if rsi >= 70 else ("oversold" if rsi <= 30 else "neutral")
            result[symbol] = {"rsi": rsi, "signal": signal}
        except Exception:
            logger.warning("Alpha Vantage RSI fetch failed for %s", symbol, exc_info=True)
        # Rate limit: AV free tier allows 5 calls/min; 1.2s spacing keeps us safe
        time.sleep(1.2)
    return result


def _fetch_twelvedata_rsi_sync(symbols: list[str], api_key: str, tracker=None) -> dict:
    """
    Fetch RSI(14) from Twelvedata for each symbol.
    Returns {symbol: {"rsi": float, "signal": "overbought"|"oversold"|"neutral"}}
    Free tier: 800 calls/day, 8 calls/min — 0.5s sleep gives margin.
    """
    import time
    if not api_key or not symbols:
        return {}
    result = {}
    base = "https://api.twelvedata.com/rsi"
    for symbol in symbols:
        try:
            with _api_ctx(tracker, "twelvedata", "rsi", symbol):
                resp = retry_get(
                    base,
                    label=f"Twelvedata {symbol}",
                    params={
                        "symbol": symbol,
                        "interval": "1day",
                        "time_period": 14,
                        "apikey": api_key,
                    },
                    timeout=15,
                )
                data = resp.json()
            # Rate limit response
            if data.get("code") == 429:
                logger.warning("Twelvedata rate limited — stopping RSI fetch")
                break
            # Error response
            if "code" in data and data["code"] != 200:
                logger.warning("Twelvedata error for %s: %s", symbol, data.get("message", ""))
                continue
            values = data.get("values")
            if not values:
                continue
            rsi = round(float(values[0]["rsi"]), 1)
            signal = "overbought" if rsi >= 70 else ("oversold" if rsi <= 30 else "neutral")
            result[symbol] = {"rsi": rsi, "signal": signal}
        except Exception:
            logger.warning("Twelvedata RSI fetch failed for %s", symbol, exc_info=True)
        # Rate limit: free tier is 8/min; 0.5s spacing gives margin
        time.sleep(0.5)
    return result


def _fetch_premarket_prices_sync(symbols: list[str], tracker=None) -> dict[str, dict]:
    """Fetch pre-market/current prices using Alpaca snapshots.

    Alpaca snapshots include the latest trade (which reflects extended hours)
    and the previous daily bar close — exactly what we need for gap analysis.

    Returns {symbol: {"premarket_price": float, "premarket_change_pct": float,
                       "prior_close": float, "has_premarket": bool}}
    """
    from .alpaca_data import fetch_snapshots_sync

    result = {}
    if not symbols:
        return result
    empty = {"premarket_price": None, "premarket_change_pct": None, "has_premarket": False}

    snaps = fetch_snapshots_sync(symbols, tracker=tracker)

    for sym in symbols:
        snap = snaps.get(sym)
        if not snap:
            result[sym] = dict(empty)
            continue
        try:
            current = snap["current_price"]
            prev_close = snap.get("prev_close")
            change_pct = round((current - prev_close) / prev_close * 100, 2) if prev_close else None
            result[sym] = {
                "premarket_price": round(current, 4),
                "premarket_change_pct": change_pct,
                "prior_close": prev_close,
                "has_premarket": True,
            }
        except Exception:
            result[sym] = dict(empty)

    return result


def _fetch_options_data_sync(symbols: list[str], tracker=None) -> dict:
    """Return put/call ratio, IV rank proxy, and 30-day implied move for each symbol."""
    result = {}
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            with _api_ctx(tracker, "yfinance", "options", symbol):
                expirations = ticker.options
            if not expirations:
                result[symbol] = None
                continue
            # Use the nearest expiration that is at least 7 days out
            current_price = ticker.history(period="1d")["Close"].iloc[-1]
            target_exp = expirations[0]
            for exp in expirations:
                exp_date = date.fromisoformat(exp)
                if (exp_date - market_today()).days >= 7:
                    target_exp = exp
                    break
            chain = ticker.option_chain(target_exp)
            calls = chain.calls
            puts = chain.puts
            total_call_oi = float(calls["openInterest"].sum()) if not calls.empty else 0
            total_put_oi = float(puts["openInterest"].sum()) if not puts.empty else 0
            pc_ratio = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else None
            # ATM IV: find strike closest to current price
            atm_iv = None
            if not calls.empty:
                calls = calls.copy()
                calls["strike_diff"] = abs(calls["strike"] - float(current_price))
                atm_row = calls.loc[calls["strike_diff"].idxmin()]
                atm_iv = round(float(atm_row.get("impliedVolatility", 0)) * 100, 1)
            # Implied 30-day move (approx): IV * sqrt(30/365)
            implied_move_pct = None
            if atm_iv is not None:
                import math
                implied_move_pct = round(atm_iv * math.sqrt(30 / 365), 1)
            result[symbol] = {
                "put_call_ratio": pc_ratio,
                "atm_iv_pct": atm_iv,
                "implied_30d_move_pct": implied_move_pct,
                "expiration_used": target_exp,
            }
        except Exception:
            logger.warning("Options data fetch failed for %s", symbol, exc_info=True)
            result[symbol] = None
    return result


def _fetch_fred_macro_sync(api_key: str, tracker=None) -> dict:
    """Fetch key macro indicators from FRED. Returns empty dict if api_key is blank."""
    if not api_key:
        return {}
    try:
        from fredapi import Fred
        fred = Fred(api_key=api_key)
        series = {
            "fed_funds_rate":    "FEDFUNDS",
            "treasury_10y":      "GS10",
            "treasury_2y":       "GS2",
            "cpi_index":         "CPIAUCSL",   # index level — YoY computed below
            "unemployment":      "UNRATE",
            "retail_sales":      "RSAFS",
            "credit_spread_hy":  "BAMLH0A0HYM2",
            "consumer_conf":     "UMCSENT",
            "pce_core":          "PCEPILFE",
            "industrial_prod":   "INDPRO",
        }
        result = {}
        cpi_series_cache = None  # cache CPI index series for YoY computation
        for label, series_id in series.items():
            try:
                with _api_ctx(tracker, "fred", "series"):
                    data = retry_call(
                        fred.get_series_latest_release, series_id,
                        label=f"FRED {series_id}",
                    )
                if data is not None and len(data) >= 2:
                    latest = float(data.iloc[-1])
                    prev = float(data.iloc[-2])
                    result[label] = {"value": round(latest, 3), "prev": round(prev, 3), "change": round(latest - prev, 3)}
                    if label == "cpi_index":
                        cpi_series_cache = data
            except Exception:
                logger.warning("FRED series %s fetch failed", series_id, exc_info=True)
        # Compute CPI YoY % change from the index level series
        # CPIAUCSL is monthly, so 12 observations back ≈ 1 year ago
        if "cpi_index" in result and cpi_series_cache is not None:
            try:
                cpi_data = cpi_series_cache
                if len(cpi_data) >= 13:
                    current_cpi = float(cpi_data.iloc[-1])
                    year_ago_cpi = float(cpi_data.iloc[-13])
                    cpi_yoy_pct = round((current_cpi - year_ago_cpi) / year_ago_cpi * 100, 2)
                    prev_cpi = float(cpi_data.iloc[-2])
                    prev_year_ago_cpi = float(cpi_data.iloc[-14]) if len(cpi_data) >= 14 else year_ago_cpi
                    prev_yoy_pct = round((prev_cpi - prev_year_ago_cpi) / prev_year_ago_cpi * 100, 2)
                    result["cpi_yoy"] = {
                        "value": cpi_yoy_pct,
                        "prev": prev_yoy_pct,
                        "change": round(cpi_yoy_pct - prev_yoy_pct, 3),
                    }
            except Exception:
                logger.warning("CPI YoY computation failed", exc_info=True)
            # Remove the raw index entry — Claude only needs the YoY rate
            result.pop("cpi_index", None)
        # Compute yield curve spread
        if "treasury_10y" in result and "treasury_2y" in result:
            spread = result["treasury_10y"]["value"] - result["treasury_2y"]["value"]
            result["yield_curve_spread_10y2y"] = round(spread, 3)
        return result
    except Exception:
        logger.warning("FRED macro fetch failed entirely", exc_info=True)
        return {}


def _fetch_market_context_sync(today: date, symbols: list[str] | None = None, tracker=None) -> str:
    lines = []
    indices = {
        "SPY": "S&P 500 ETF",
        "QQQ": "Nasdaq 100 ETF",
        "IWM": "Russell 2000 ETF",
        "^VIX": "VIX (fear index)",
    }
    lines.append("=== MARKET CONTEXT ===")
    lines.append(f"Date: {today}")
    lines.append("\nIndex levels:")
    for ticker_sym, label in indices.items():
        try:
            t = yf.Ticker(ticker_sym)
            with _api_ctx(tracker, "yfinance", "market_context"):
                hist = t.history(period="5d")
            if hist.empty:
                continue
            price = round(float(hist["Close"].iloc[-1]), 2)
            prev = round(float(hist["Close"].iloc[-2]), 2) if len(hist) >= 2 else price
            chg_pct = round((price - prev) / prev * 100, 2) if prev else 0
            week_ago = round(float(hist["Close"].iloc[0]), 2)
            week_chg = round((price - week_ago) / week_ago * 100, 2) if week_ago else 0
            lines.append(f"  {label}: {price:.2f} ({chg_pct:+.2f}% today, {week_chg:+.2f}% 5d)")
        except Exception:
            logger.warning("Market context: index %s fetch failed", ticker_sym, exc_info=True)
    sectors = {
        "XLK": "Tech", "XLF": "Financials", "XLE": "Energy",
        "XLV": "Healthcare", "XLI": "Industrials",
    }
    lines.append("\nSector performance (1-week):")
    for ticker_sym, label in sectors.items():
        try:
            t = yf.Ticker(ticker_sym)
            with _api_ctx(tracker, "yfinance", "market_context"):
                hist = t.history(period="5d")
            if hist.empty:
                continue
            price = float(hist["Close"].iloc[-1])
            week_ago = float(hist["Close"].iloc[0])
            chg = round((price - week_ago) / week_ago * 100, 2) if week_ago else 0
            lines.append(f"  {label} ({ticker_sym}): {chg:+.2f}%")
        except Exception:
            logger.warning("Market context: sector %s fetch failed", ticker_sym, exc_info=True)
    window_start = today - timedelta(days=1)
    window_end = today + timedelta(days=2)
    upcoming_earnings = []
    for symbol in (symbols or WATCHLIST):
        try:
            t = yf.Ticker(symbol)
            cal = t.calendar
            if cal is None:
                continue
            earnings_dates = cal.get("Earnings Date")
            if not earnings_dates:
                continue
            if not isinstance(earnings_dates, list):
                earnings_dates = [earnings_dates]
            for ed in earnings_dates:
                try:
                    ed_date = ed.date() if hasattr(ed, "date") else date.fromisoformat(str(ed)[:10])
                    if window_start <= ed_date <= window_end:
                        upcoming_earnings.append((symbol, ed_date))
                        break
                except (ValueError, TypeError, AttributeError):
                    logger.debug("Earnings date parse failed for %s", symbol)
        except Exception:
            logger.debug("Earnings calendar unavailable for %s", symbol)
    if upcoming_earnings:
        lines.append("\nUpcoming earnings (watchlist, ±2 days):")
        for symbol, ed in sorted(upcoming_earnings, key=lambda x: x[1]):
            marker = " ← TODAY" if ed == today else ""
            lines.append(f"  {symbol}: reports {ed}{marker}")
    else:
        lines.append("\nNo watchlist earnings within ±2 days.")
    try:
        spy_news = yf.Ticker("SPY").news or []
        if spy_news:
            lines.append("\nRecent broad market news:")
            for item in spy_news[:6]:
                title = item.get("content", {}).get("title", "")
                if title:
                    lines.append(f"  - {title}")
    except Exception:
        logger.warning("SPY broad market news fetch failed", exc_info=True)
    return "\n".join(lines)


# S&P 500 members not already in WATCHLIST — used as the screener candidate pool.
# Update periodically as index composition changes. Excludes all 40 WATCHLIST symbols.
_SP500_POOL = [
    "A", "AAL", "AAP", "ABBV", "ABC", "ACGL", "ACN", "ADSK", "AEE", "AEP",
    "AES", "AFL", "AIG", "AIZ", "AJG", "AKAM", "ALB", "ALGN", "ALL", "ALLY",
    "ALNY", "ALSN", "ALT", "AMCR", "AME", "AMGN", "AMP", "AMT", "AMTM", "AON",
    "AOS", "APD", "APH", "APO", "ARE", "ATO", "AVB", "AVTR", "AVY", "AWK",
    "AXON", "AXP", "AZO", "BAC", "BAX", "BBY", "BDX", "BIIB", "BIO", "BK",
    "BMY", "BR", "BRK-B", "BSX", "BXP", "C", "CAG", "CAH", "CARR", "CAT",
    "CB", "CBOE", "CBRE", "CCL", "CDNS", "CDW", "CE", "CF", "CFG", "CHD",
    "CHRW", "CHTR", "CI", "CINF", "CL", "CLX", "CMA", "CMCSA", "CME", "CMG",
    "CMI", "CMS", "CNC", "CNP", "COF", "COO", "COP", "CPB", "CPRT", "CPT",
    "CRL", "CSX", "CTAS", "CTLT", "CTSH", "CTVA", "CVS", "D", "DAL", "DD",
    "DE", "DECK", "DG", "DGX", "DHI", "DHR", "DIS", "DLTR", "DOC", "DOV",
    "DOW", "DPZ", "DRI", "DTE", "DUK", "DVA", "DVN", "DXCM", "EA", "EBAY",
    "ECL", "ED", "EFX", "EG", "EIX", "EL", "ELV", "EMN", "EMR", "ENPH",
    "EOG", "EPAM", "EQR", "EQT", "ES", "ESS", "ETN", "ETR", "EVRG", "EW",
    "EXC", "EXPE", "EXR", "F", "FANG", "FAST", "FCX", "FDS", "FDX", "FE",
    "FFIV", "FI", "FICO", "FIS", "FITB", "FLT", "FMC", "FOX", "FOXA", "FRT",
    "FTNT", "FTV", "GD", "GDDY", "GE", "GEHC", "GEN", "GEV", "GILD", "GIS",
    "GL", "GLW", "GM", "GNRC", "GPC", "GPN", "GRMN", "GWW", "HAL", "HAS",
    "HBAN", "HCA", "HES", "HIG", "HII", "HLT", "HOLX", "HOOD", "HON", "HPE",
    "HPQ", "HRL", "HSIC", "HST", "HSY", "HUM", "HWM", "IDXX", "IEX", "IFF",
    "INCY", "INTC", "INTU", "INVH", "IP", "IPG", "IQV", "IR", "IRM", "IT",
    "ITW", "IVZ", "J", "JBHT", "JBL", "JKHY", "JNJ", "JNPR", "K", "KDP",
    "KEY", "KEYS", "KHC", "KIM", "KKR", "KLAC", "KMB", "KMI", "KMX", "KR",
    "KVUE", "L", "LDOS", "LEN", "LH", "LIN", "LKQ", "LLY", "LMT", "LNC",
    "LNT", "LOW", "LRCX", "LULU", "LUV", "LVS", "LW", "LYB", "LYV", "MAA",
    "MAR", "MAS", "MCD", "MCHP", "MCK", "MCO", "MDLZ", "MDT", "MGM", "MHK",
    "MKC", "MKTX", "MLM", "MMC", "MMM", "MNST", "MOH", "MOS", "MPC", "MPWR",
    "MRO", "MS", "MSCI", "MTB", "MTCH", "MTD", "MU", "NCLH", "NDAQ", "NEE",
    "NEM", "NFLX", "NI", "NKE", "NOC", "NOW", "NRG", "NSC", "NTAP", "NTRS",
    "NUE", "NVR", "NWS", "NWSA", "NXPI", "O", "OKE", "ON", "ORCL", "OTIS",
    "OXY", "PANW", "PARA", "PAYC", "PAYX", "PCAR", "PCG", "PEG", "PFE", "PFG",
    "PGR", "PH", "PHM", "PKG", "PLD", "PM", "PNC", "PNR", "PNW", "PODD",
    "POOL", "PPG", "PPL", "PRU", "PSA", "PSX", "PWR", "PYPL", "QCOM", "QRVO",
    "RCL", "REG", "REGN", "RF", "RJF", "RL", "RMD", "ROK", "ROL", "ROP",
    "ROST", "RSG", "RTX", "RVTY", "SBAC", "SBUX", "SHW", "SJM", "SLB", "SMCI",
    "SNA", "SNPS", "SO", "SPG", "SPGI", "SRE", "STE", "STLD", "STT", "STX",
    "STZ", "SWK", "SWKS", "SYF", "SYK", "SYY", "T", "TAP", "TDG", "TDY",
    "TECH", "TEL", "TER", "TFC", "TFX", "TGT", "TJX", "TMO", "TMUS", "TPR",
    "TRGP", "TRMB", "TROW", "TRV", "TSCO", "TSN", "TT", "TTD", "TXN", "TXT",
    "TYL", "UAL", "UBER", "UDR", "UHS", "ULTA", "UNP", "UPS", "URI", "USB",
    "USDP", "VFC", "VICI", "VLO", "VMC", "VRSK", "VRSN", "VRTX", "VTR", "VTRS",
    "VZ", "WAB", "WAT", "WBA", "WBD", "WDC", "WEC", "WELL", "WFC", "WHR",
    "WM", "WMB", "WRB", "WST", "WTW", "WY", "WYNN", "XEL", "XOM", "XYL",
    "YUM", "ZBH", "ZBRA", "ZTS",
]


def _fetch_momentum_screener_sync(n: int = 20, tracker=None) -> list[str]:
    """
    Return up to n symbols from the S&P 500 pool that are:
      - Not already in WATCHLIST
      - Price > 20-day moving average
      - Average daily volume > 1M shares
      - Ranked by 5-day price momentum (top n)

    Primary source: Alpaca bars (stable, ~5-10s for 500 symbols).
    Fallback: yfinance batch download if Alpaca returns nothing.
    Returns [] on total failure so the pipeline continues with just the
    static watchlist.
    """
    screen_symbols = [s for s in _SP500_POOL if s not in WATCHLIST]
    if not screen_symbols:
        return []

    from .alpaca_data import fetch_bars_sync

    # Alpaca uses "." for class-suffix tickers (BRK.B, BF.B) while Yahoo uses "-".
    # Normalize for Alpaca, then map results back to Yahoo-style symbols used
    # elsewhere in the pipeline.
    def _to_alpaca(sym: str) -> str:
        return sym.replace("-", ".")

    alpaca_to_yahoo = {_to_alpaca(s): s for s in screen_symbols}
    alpaca_symbols = list(alpaca_to_yahoo.keys())

    bars = {}
    try:
        with _api_ctx(tracker, "alpaca_data", "screener_bars", "SP500"):
            # 90 days covers 20d MA and 5d momentum with weekends/holidays buffer
            raw_bars = fetch_bars_sync(alpaca_symbols, days=90, tracker=None)
        # Map Alpaca symbols back to the Yahoo-style keys we use everywhere else
        bars = {alpaca_to_yahoo.get(s, s): v for s, v in raw_bars.items()}
    except Exception:
        logger.warning("Alpaca screener bars failed — falling back to yfinance", exc_info=True)

    if not bars or sum(len(b) for b in bars.values()) == 0:
        logger.warning("Alpaca returned no bars — falling back to yfinance batch")
        try:
            with _api_ctx(tracker, "yfinance", "screener_batch", "SP500"):
                df = yf.download(
                    screen_symbols,
                    period="3mo",
                    interval="1d",
                    progress=False,
                    group_by="ticker",
                    threads=True,
                )
        except Exception:
            logger.warning("yfinance screener fallback also failed", exc_info=True)
            return []
        candidates = []
        for symbol in screen_symbols:
            try:
                if len(screen_symbols) == 1:
                    sym_close = df["Close"]
                    sym_vol = df["Volume"]
                else:
                    sym_close = df[(symbol, "Close")].dropna()
                    sym_vol = df[(symbol, "Volume")].dropna()
                if len(sym_close) < 25:
                    continue
                current = float(sym_close.iloc[-1])
                ma20 = float(sym_close.tail(20).mean())
                avg_vol = float(sym_vol.tail(20).mean())
                momentum_5d = (current - float(sym_close.iloc[-6])) / float(sym_close.iloc[-6]) * 100
                if current > ma20 and avg_vol > 1_000_000:
                    candidates.append((symbol, momentum_5d))
            except Exception:
                continue
        candidates.sort(key=lambda x: x[1], reverse=True)
        return [sym for sym, _ in candidates[:n]]

    # Primary path: Alpaca bars
    candidates = []
    for symbol, sym_bars in bars.items():
        try:
            if len(sym_bars) < 25:
                continue
            closes = [b["close"] for b in sym_bars]
            volumes = [b["volume"] for b in sym_bars]
            current = closes[-1]
            ma20 = sum(closes[-20:]) / 20
            avg_vol = sum(volumes[-20:]) / 20
            ref = closes[-6]
            if ref <= 0:
                continue
            momentum_5d = (current - ref) / ref * 100
            if current > ma20 and avg_vol > 1_000_000:
                candidates.append((symbol, momentum_5d))
        except Exception:
            logger.debug("Screener: skipping %s (data extraction failed)", symbol)
            continue

    candidates.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _ in candidates[:n]]


def _fetch_opening_prices_sync(symbols: list[str], trade_date: date, tracker=None) -> dict[str, float | None]:
    """Fetch today's opening prices using Alpaca snapshots.

    The snapshot's daily_bar.open gives us the official market open price.
    Falls back to yfinance if Alpaca fails.
    """
    from .alpaca_data import fetch_snapshots_sync

    results: dict[str, float | None] = {}
    snaps = fetch_snapshots_sync(symbols, tracker=tracker)

    for symbol in symbols:
        snap = snaps.get(symbol)
        if snap and snap.get("daily_open") is not None:
            results[symbol] = round(snap["daily_open"], 4)
        else:
            # Fallback to yfinance for symbols Alpaca doesn't cover
            try:
                date_str = trade_date.isoformat()
                next_day = (trade_date + timedelta(days=1)).isoformat()
                with _api_ctx(tracker, "yfinance", "opening_prices", symbol):
                    df = yf.download(symbol, start=date_str, end=next_day,
                                     interval="1m", progress=False, auto_adjust=True)
                if not df.empty:
                    results[symbol] = round(float(df["Open"].values.flat[0]), 4)
                else:
                    results[symbol] = None
            except Exception:
                logger.warning("Opening price fetch failed for %s", symbol, exc_info=True)
                results[symbol] = None
    return results


_FRED_EXTRA_LABELS = {
    "retail_sales":     "Retail Sales",
    "credit_spread_hy": "HY Credit Spread (OAS)",
    "consumer_conf":    "Consumer Sentiment (UMich)",
    "pce_core":         "Core PCE",
    "industrial_prod":  "Industrial Production",
}


def _score_symbol(symbol: str, price_data: dict, news_data: dict, polygon_news: dict | None,
                   technicals: dict | None, insider_activity: dict | None,
                   relative_strength: dict | None) -> float:
    """Score a symbol for relevance. Higher = more interesting for LLM review."""
    score = 0.0
    data = price_data.get(symbol, {})

    # Momentum: reward 3-8% weekly moves in the trade direction.
    # Down-moves get no score — strategy only enters longs, so a falling stock
    # wastes a top-25 pre-filter slot even if its magnitude is in the sweet spot.
    wk = data.get("week_change_pct", 0) or 0
    if 3 <= wk <= 8:
        score += 3.0
    elif wk > 1:
        score += 1.0

    # News catalyst: symbols with news get a boost
    poly = (polygon_news or {}).get(symbol, [])
    yf_news = news_data.get(symbol, [])
    if poly:
        score += 2.0
    elif yf_news:
        score += 1.0

    # Technical alignment: MACD bullish or golden cross
    t = (technicals or {}).get(symbol, {})
    macd = t.get("macd", {})
    if macd and macd.get("signal") == "bullish":
        score += 1.5
    ma = t.get("ma_crossover", {})
    if ma and ma.get("signal") in ("golden_cross", "above_both"):
        score += 1.0

    # Volume confirmation
    vol = t.get("volume", {})
    if vol and vol.get("signal") == "high_volume":
        score += 1.0

    # Relative strength vs sector
    rs = (relative_strength or {}).get(symbol)
    if rs is not None and rs > 2.0:
        score += 1.5
    elif rs is not None and rs > 0:
        score += 0.5

    # Insider activity
    ia = (insider_activity or {}).get(symbol, {})
    if ia.get("form4_filings", 0) > 0 or ia.get("recent_buys", 0) > 0:
        score += 0.5

    return score


MAX_CONTEXT_SYMBOLS = 25  # top N non-held symbols to include in LLM context


def build_research_context(
    portfolio_dict: dict,
    price_data: dict,
    news_data: dict,
    current_symbols: list[str],
    earnings_surprise: dict | None = None,
    insider_activity: dict | None = None,
    fred_macro: dict | None = None,
    polygon_news: dict | None = None,
    av_technicals: dict | None = None,
    technicals: dict | None = None,
    analyst_consensus: dict | None = None,
    relative_strength: dict | None = None,
    premarket_data: dict | None = None,
    twelvedata_rsi: dict | None = None,
    economic_calendar_context: str | None = None,
) -> str:
    # Pre-filter: score all symbols, keep top N + held positions
    all_symbols = list(price_data.keys())
    held_set = set(current_symbols)
    non_held = [s for s in all_symbols if s not in held_set]

    scores = {s: _score_symbol(s, price_data, news_data, polygon_news,
                                technicals, insider_activity, relative_strength)
              for s in non_held}
    top_non_held = sorted(scores, key=scores.get, reverse=True)[:MAX_CONTEXT_SYMBOLS]
    filtered_symbols = set(current_symbols) | set(top_non_held)

    # Filter price_data to only include relevant symbols
    filtered_price_data = {s: d for s, d in price_data.items() if s in filtered_symbols}

    logger.info("Pre-filter: %d total → %d symbols (%d held + %d top-scored)",
                len(all_symbols), len(filtered_symbols), len(held_set), len(top_non_held))

    lines = []

    # FRED macro section
    if fred_macro:
        lines.append("=== MACRO INDICATORS (FRED) ===")
        if "fed_funds_rate" in fred_macro:
            d = fred_macro["fed_funds_rate"]
            lines.append(f"Fed Funds Rate: {d['value']}% (prev {d['prev']}%, chg {d['change']:+.3f}%)")
        if "yield_curve_spread_10y2y" in fred_macro:
            spread = fred_macro["yield_curve_spread_10y2y"]
            signal = "INVERTED (recession signal)" if spread < 0 else "normal"
            lines.append(f"10Y-2Y Yield Spread: {spread:+.3f}% ({signal})")
        if "cpi_yoy" in fred_macro:
            d = fred_macro["cpi_yoy"]
            lines.append(f"CPI YoY: {d['value']}% (prev {d['prev']}%, chg {d['change']:+.3f}pp)")
        if "unemployment" in fred_macro:
            d = fred_macro["unemployment"]
            lines.append(f"Unemployment: {d['value']}% (prev {d['prev']}%)")
        for key, label in _FRED_EXTRA_LABELS.items():
            if key in fred_macro:
                d = fred_macro[key]
                lines.append(f"{label}: {d['value']} (prev {d['prev']}, chg {d['change']:+.3f})")
        lines.append("")

    # Economic calendar section (top of context so Claude sees macro events first)
    if economic_calendar_context:
        lines.append("=== UPCOMING ECONOMIC RELEASES ===")
        lines.append(economic_calendar_context)
        lines.append("")

    # Analyst consensus section
    if analyst_consensus:
        from .finnhub_data import build_analyst_context
        analyst_text = build_analyst_context(analyst_consensus)
        if analyst_text:
            lines.append(analyst_text)
            lines.append("")

    # Portfolio state
    lines.append("=== PORTFOLIO ===")
    lines.append(f"Cash: ${portfolio_dict['cash_balance']:,.2f}")
    lines.append(f"Total Value: ${portfolio_dict['total_value']:,.2f}")
    if portfolio_dict["positions"]:
        lines.append("Current Positions:")
        for pos in portfolio_dict["positions"]:
            gain_str = f"{pos['unrealized_gain']:+.2f}" if pos["unrealized_gain"] else "n/a"
            lines.append(
                f"  {pos['symbol']}: {pos['shares']} shares @ ${pos['avg_cost_basis']:.2f} "
                f"(now ${pos['current_price']:.2f}, P&L {gain_str}, "
                f"{pos['days_held']}d held, {pos['tax_category']})"
            )
    lines.append("")

    # Per-stock data (pre-filtered to top candidates + held positions)
    lines.append(f"=== WATCHLIST DATA ({len(filtered_price_data)} symbols, filtered from {len(price_data)}) ===")
    for symbol, data in sorted(filtered_price_data.items()):
        is_held = symbol in current_symbols
        held_marker = " [HELD]" if is_held else ""
        lines.append(f"\n{symbol}{held_marker}:")
        rs_str = ""
        if relative_strength and symbol in relative_strength and relative_strength[symbol] is not None:
            rs = relative_strength[symbol]
            rs_label = "outperforming" if rs > 0 else "underperforming"
            rs_str = f" | vs sector: {rs:+.1f}% ({rs_label})"
        lines.append(f"  Price: ${data['current_price']:.2f} | 1wk: {data['week_change_pct']:+.1f}% | 1mo: {data['month_change_pct']:+.1f}%{rs_str}")
        lines.append(f"  52w range: ${data['low_52w']:.2f} – ${data['high_52w']:.2f}")
        if premarket_data and symbol in premarket_data:
            pm = premarket_data[symbol]
            if pm.get("has_premarket") and pm.get("premarket_price") is not None:
                pm_change = pm.get("premarket_change_pct")
                pm_str = f"  Pre-market: ${pm['premarket_price']:.2f}"
                if pm_change is not None:
                    pm_str += f" ({pm_change:+.1f}% from close)"
                    if abs(pm_change) > 5:
                        pm_str += " ⚠ GAP >5%"
                lines.append(pm_str)
        if data.get("pe_ratio"):
            lines.append(f"  P/E: {data['pe_ratio']:.1f} | Fwd P/E: {data.get('forward_pe', 'n/a')}")
        if data.get("short_ratio") or data.get("short_percent_float"):
            short_pct = f"{data['short_percent_float']*100:.1f}%" if data.get("short_percent_float") else "n/a"
            lines.append(f"  Short ratio: {data.get('short_ratio', 'n/a')} | Short % float: {short_pct}")

        # Earnings surprise history
        if earnings_surprise and symbol in earnings_surprise:
            surprises = earnings_surprise[symbol]
            if surprises:
                summary = ", ".join(f"{s['verdict']}({s['surprise_pct']:+.1f}%)" for s in surprises)
                lines.append(f"  EPS last 4Q: {summary}")

        # Insider activity
        if insider_activity and symbol in insider_activity:
            ia = insider_activity[symbol]
            form4s = ia.get("form4_filings", 0)
            if form4s > 0:
                lines.append(f"  Insider: {form4s} Form 4 filing(s) in last 30d (transaction type unknown from EDGAR)")
            elif ia["recent_buys"] > 0 or ia["recent_sells"] > 0:
                lines.append(f"  Insider: {ia['recent_buys']:,} shares bought, {ia['recent_sells']:,} shares sold (recent)")

        # RSI: prefer Twelvedata (full watchlist) over Alpha Vantage (screener-only, ≤20 symbols)
        rsi_td = (twelvedata_rsi or {}).get(symbol)
        rsi_av = (av_technicals or {}).get(symbol)
        rsi_src = rsi_td if rsi_td is not None else rsi_av
        if rsi_src is not None:
            lines.append(f"  RSI(14): {rsi_src['rsi']} [{rsi_src['signal'].upper()}]")

        # Technical analysis (MACD, Bollinger, MA crossover, S/R, volume)
        if technicals and symbol in technicals:
            t = technicals[symbol]
            ta_parts = []
            if "macd" in t:
                m = t["macd"]
                ta_parts.append(f"MACD: {m['signal'].upper()} (hist={m['histogram']:+.4f})")
            if "bollinger" in t:
                b = t["bollinger"]
                ta_parts.append(f"BB: {b['signal'].upper()} (%B={b['pct_b']:.2f}, band=${b['lower']:.0f}-${b['upper']:.0f})")
            if "ma_crossover" in t:
                ma = t["ma_crossover"]
                ta_parts.append(f"MA: {ma['signal'].upper()} (50d=${ma['ma_50']:.0f}, 200d=${ma['ma_200']:.0f})")
            if "support_resistance" in t:
                sr = t["support_resistance"]
                ta_parts.append(f"S/R: ${sr['support']:.0f} / ${sr['resistance']:.0f}")
            if "volume" in t:
                v = t["volume"]
                ta_parts.append(f"Vol: {v['signal'].upper()} (rel={v['relative_volume']:.1f}x)")
            if t.get("atr"):
                a = t["atr"]
                ta_parts.append(f"ATR: ${a['atr']:.2f} ({a['atr_pct']:.1f}%)")
            if ta_parts:
                lines.append(f"  Technicals: {' | '.join(ta_parts)}")

        # News — prefer Polygon (with descriptions if available); fall back to yfinance
        poly_articles = (polygon_news or {}).get(symbol, [])
        yf_headlines = news_data.get(symbol, [])
        if poly_articles:
            lines.append("  News:")
            for a in poly_articles[:3]:
                if isinstance(a, dict):
                    title = a.get("title", "")
                    desc = a.get("description", "")
                    if desc:
                        lines.append(f"    - {title}: {desc[:300]}")
                    else:
                        lines.append(f"    - {title}")
                else:
                    lines.append(f"    - {a}")
        elif yf_headlines:
            lines.append("  News:")
            for h in yf_headlines[:3]:
                lines.append(f"    - {h}")

    # Data quality notes for Claude
    missing = []
    if not price_data: missing.append("price data")
    if not news_data: missing.append("news")
    if not polygon_news: missing.append("detailed news (Alpaca)")
    if not analyst_consensus: missing.append("analyst consensus")
    if not fred_macro: missing.append("FRED macro")
    if not av_technicals: missing.append("Alpha Vantage technicals")
    if not insider_activity: missing.append("insider activity")
    if not earnings_surprise: missing.append("earnings surprise")
    if missing:
        lines.append(f"\n## Data Availability Note\nThe following data sources returned no data: {', '.join(missing)}. Analysis should account for these gaps.")

    return "\n".join(lines)


def build_options_context(options_data: dict) -> str:
    """Format options data for the decision prompt (Call 2)."""
    if not options_data:
        return ""
    lines = ["=== OPTIONS DATA (CANDIDATES) ==="]
    for symbol, data in options_data.items():
        if data is None:
            lines.append(f"{symbol}: options data unavailable")
            continue
        lines.append(f"{symbol}:")
        lines.append(f"  Put/Call ratio: {data['put_call_ratio'] or 'n/a'}")
        lines.append(f"  ATM IV: {data['atm_iv_pct'] or 'n/a'}%")
        lines.append(f"  Implied 30d move: ±{data['implied_30d_move_pct'] or 'n/a'}%")
        lines.append(f"  (expiry used: {data['expiration_used']})")
    return "\n".join(lines)


# ── Sector relative strength ────────────────────────────────────────────────

# Map symbols to sector ETFs for relative strength calculation
_SECTOR_ETF_MAP = {
    # Tech
    **{s: "XLK" for s in ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "ADBE",
                           "NOW", "PANW", "CRWD", "DDOG", "PLTR", "INTC", "QCOM", "MU",
                           "NXPI", "KLAC", "LRCX", "MCHP", "SNPS", "CDNS", "FTNT", "TXN",
                           "ANET", "FICO", "IT", "KEYS"]},
    # Communication Services
    **{s: "XLC" for s in ["GOOGL", "META", "NFLX", "DIS", "TMUS", "T", "VZ", "EA",
                           "TTWO", "WBD", "PARA", "FOX", "FOXA", "MTCH", "LYV", "NWSA"]},
    # Consumer Discretionary
    **{s: "XLY" for s in ["AMZN", "TSLA", "HD", "COST", "WMT", "TJX", "NKE", "SBUX",
                           "LOW", "TGT", "ROST", "LULU", "DHI", "LEN", "GM", "F",
                           "UBER", "HOOD", "SHOP", "PYPL", "EBAY", "BKNG", "MAR", "HLT"]},
    # Financials
    **{s: "XLF" for s in ["JPM", "V", "MA", "GS", "BLK", "SPGI", "SCHW", "ICE",
                           "MS", "WFC", "BAC", "C", "AXP", "BRK-B", "PNC", "USB",
                           "CME", "MCO", "MMC", "AIG", "MET", "PRU", "TFC", "KKR"]},
    # Healthcare
    **{s: "XLV" for s in ["UNH", "JNJ", "ABBV", "LLY", "MRK", "PFE", "TMO", "ABT",
                           "DHR", "BMY", "AMGN", "GILD", "ISRG", "VRTX", "REGN", "MDT",
                           "SYK", "BSX", "EW", "HCA", "CI", "HUM", "MCK", "CAH"]},
    # Energy
    **{s: "XLE" for s in ["XOM", "CVX", "COP", "SLB", "HAL", "OXY", "EOG", "MPC",
                           "PSX", "VLO", "DVN", "FANG", "HES", "OKE", "WMB", "KMI"]},
    # Industrials
    **{s: "XLI" for s in ["BA", "CAT", "HON", "GE", "RTX", "DE", "UNP", "UPS",
                           "LMT", "NOC", "GD", "MMM", "EMR", "ETN", "ITW", "PH",
                           "FDX", "CSX", "NSC", "WM", "RSG", "URI", "IR", "TT"]},
    # Real Estate
    **{s: "XLRE" for s in ["PLD", "AMT", "CCI", "EQIX", "SPG", "O", "DLR", "PSA",
                            "WELL", "EQR", "AVB", "MAA", "UDR", "VTR", "IRM"]},
    # Utilities
    **{s: "XLU" for s in ["NEE", "SO", "DUK", "D", "AEP", "EXC", "SRE", "PCG",
                           "ED", "WEC", "ES", "XEL", "PEG", "EIX", "DTE", "ETR"]},
    # Materials
    **{s: "XLB" for s in ["LIN", "APD", "SHW", "ECL", "FCX", "NEM", "NUE", "VMC",
                           "MLM", "DOW", "DD", "PPG", "IP", "FMC", "CF", "MOS"]},
    # Consumer Staples
    **{s: "XLP" for s in ["PG", "KO", "PEP", "PM", "MO", "CL", "MDLZ", "KHC",
                           "GIS", "SYY", "HSY", "K", "KDP", "STZ", "MKC", "CLX"]},
    # Catch-all: COIN, NET, SNOW mapped to broad market
    **{s: "SPY" for s in ["COIN", "NET", "SNOW"]},
}

_SECTOR_ETFS = sorted(set(_SECTOR_ETF_MAP.values()))


def _fetch_sector_returns_sync(tracker=None) -> dict[str, float]:
    """Fetch 5-day returns for all sector ETFs using Alpaca bars.

    Returns {etf: pct_return}.
    """
    from .alpaca_data import fetch_bars_sync

    result = {}
    try:
        bars_data = fetch_bars_sync(_SECTOR_ETFS, days=15, tracker=tracker)
        for etf in _SECTOR_ETFS:
            bars = bars_data.get(etf, [])
            if len(bars) >= 5:
                current = bars[-1]["close"]
                five_ago = bars[-5]["close"]
                result[etf] = round((current - five_ago) / five_ago * 100, 2)
    except Exception:
        logger.warning("Sector ETF fetch failed", exc_info=True)
    return result


def compute_relative_strength(price_data: dict, sector_returns: dict) -> dict[str, float | None]:
    """Compute relative strength: stock 5d return minus sector ETF 5d return.

    Positive = outperforming sector. Negative = underperforming.
    """
    result = {}
    for symbol, data in price_data.items():
        stock_return = data.get("week_change_pct", 0)
        etf = _SECTOR_ETF_MAP.get(symbol)
        if etf and etf in sector_returns:
            result[symbol] = round(stock_return - sector_returns[etf], 2)
        else:
            result[symbol] = None
    return result


# ── Async wrappers ──────────────────────────────────────────────────────────

async def fetch_price_data(symbols: list[str], tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_price_data_sync(symbols, tracker=tracker))


async def fetch_news(symbols: list[str], tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_news_sync(symbols, tracker=tracker))


async def fetch_earnings_surprise(symbols: list[str], tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_earnings_surprise_sync(symbols, tracker=tracker))


async def fetch_insider_activity(symbols: list[str], tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_insider_activity_sync(symbols, tracker=tracker))


async def fetch_options_data(symbols: list[str], tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_options_data_sync(symbols, tracker=tracker))


async def fetch_fred_macro(api_key: str, tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_fred_macro_sync(api_key, tracker=tracker))


async def fetch_market_context(today: date, symbols: list[str] | None = None, tracker=None) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_market_context_sync(today, symbols, tracker=tracker))


async def fetch_edgar_insider(symbols: list[str], days_back: int = 30, tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_edgar_insider_sync(symbols, days_back, tracker=tracker))


async def fetch_polygon_news(symbols: list[str], api_key: str, tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_polygon_news_sync(symbols, api_key, tracker=tracker))


async def fetch_av_technicals(symbols: list[str], api_key: str, tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_av_technicals_sync(symbols, api_key, tracker=tracker))


async def fetch_twelvedata_rsi(symbols: list[str], api_key: str, tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_twelvedata_rsi_sync(symbols, api_key, tracker=tracker))


async def fetch_momentum_screener(n: int = 20, tracker=None) -> list[str]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_momentum_screener_sync(n, tracker=tracker))


async def fetch_sector_returns(tracker=None) -> dict[str, float]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_sector_returns_sync(tracker=tracker))


async def fetch_premarket_prices(symbols: list[str], tracker=None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_premarket_prices_sync(symbols, tracker=tracker))


async def fetch_opening_prices(symbols: list[str], trade_date: date, tracker=None) -> dict[str, float | None]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_opening_prices_sync(symbols, trade_date, tracker=tracker))


def _fetch_market_eod_sync(target_date: date) -> dict:
    """Fetch end-of-day performance for major indices and S&P sector ETFs.

    Uses Alpaca snapshots for sector ETFs (reliable).
    Keeps yfinance for indices (^GSPC, ^IXIC, etc.) since Alpaca doesn't
    cover index symbols — only ETFs and equities.
    """
    from .alpaca_data import fetch_snapshots_sync

    indices = {
        "S&P 500": "^GSPC",
        "NASDAQ": "^IXIC",
        "Dow Jones": "^DJI",
        "Russell 2000": "^RUT",
    }
    sectors = {
        "XLK": "Tech", "XLF": "Finance", "XLE": "Energy", "XLV": "Health",
        "XLI": "Industrials", "XLP": "Staples", "XLY": "Cons Disc",
        "XLB": "Materials", "XLC": "Comm Svcs", "XLU": "Utilities", "XLRE": "Real Estate",
    }

    result: dict = {"indices": {}, "sectors": {}}

    # Indices: yfinance (Alpaca doesn't have ^GSPC etc.)
    for label, symbol in indices.items():
        try:
            hist = yf.Ticker(symbol).history(period="2d", interval="1d")
            if len(hist) >= 2:
                close = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2])
                pct = round((close - prev) / prev * 100, 2)
                result["indices"][label] = {"symbol": symbol, "price": round(close, 2), "change_pct": pct}
        except Exception:
            logger.debug("Index EOD fetch failed for %s", symbol)

    # Sectors: Alpaca snapshots (much more reliable than per-ticker yfinance)
    sector_symbols = list(sectors.keys())
    snaps = fetch_snapshots_sync(sector_symbols)
    for symbol, label in sectors.items():
        snap = snaps.get(symbol)
        if snap and snap.get("current_price") and snap.get("prev_close"):
            close = snap["current_price"]
            prev = snap["prev_close"]
            pct = round((close - prev) / prev * 100, 2)
            result["sectors"][symbol] = {"label": label, "change_pct": pct}

    return result


async def fetch_market_eod(target_date: date) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_market_eod_sync, target_date)
