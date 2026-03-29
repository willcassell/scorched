"""Stock research: price data, fundamentals, news, macro, and market context via yfinance + FRED."""
import asyncio
from datetime import date, timedelta
from decimal import Decimal

import yfinance as yf

# Universe of large-cap, liquid stocks to consider each morning
WATCHLIST = [
    # Core 30
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "V", "MA",
    "UNH", "JNJ", "XOM", "CVX", "WMT", "HD", "PG", "KO", "PEP", "ABBV",
    "LLY", "MRK", "COST", "AVGO", "ORCL", "ADBE", "CRM", "AMD", "NFLX", "BA",
    # 10 additions: broad sector coverage, high liquidity
    "GS", "BLK", "SPGI", "NOW", "PANW", "UBER", "SCHW", "ICE", "DIS", "HOOD",
]


def _fetch_price_data_sync(symbols: list[str]) -> dict:
    result = {}
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1y")
            info = ticker.info
            if hist.empty:
                continue
            # Use fast_info for real-time last price; fall back to daily close
            try:
                current_price = float(ticker.fast_info["last_price"])
            except Exception:
                current_price = float(hist["Close"].iloc[-1])
            week_ago_price = float(hist["Close"].iloc[-5]) if len(hist) >= 5 else current_price
            month_ago_price = float(hist["Close"].iloc[-22]) if len(hist) >= 22 else float(hist["Close"].iloc[0])
            high_52w = float(info.get("fiftyTwoWeekHigh", 0))
            low_52w = float(info.get("fiftyTwoWeekLow", 0))
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
                "insider_buy_pct": None,  # populated separately
                "history_close": [float(x) for x in hist["Close"].tolist()],
                "history_volume": [float(x) for x in hist["Volume"].tolist()],
            }
        except Exception:
            pass
    return result


def _fetch_news_sync(symbols: list[str]) -> dict:
    result = {}
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            news = ticker.news or []
            headlines = []
            for item in news[:5]:
                title = item.get("content", {}).get("title", "")
                if title:
                    headlines.append(title)
            result[symbol] = headlines
        except Exception:
            result[symbol] = []
    return result


def _fetch_earnings_surprise_sync(symbols: list[str]) -> dict:
    """Return last 4 quarters of EPS beat/miss for each symbol."""
    result = {}
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
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
            result[symbol] = {"recent_buys": 0, "recent_sells": 0}
    return result


def _build_ticker_to_cik_map(headers: dict) -> dict[str, str]:
    """
    Download the SEC company tickers JSON and return a {TICKER: zero-padded CIK} map.
    This is a single ~2MB request that covers all SEC-registered companies.
    """
    import requests
    try:
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            entry["ticker"].upper(): str(entry["cik_str"]).zfill(10)
            for entry in data.values()
        }
    except Exception:
        return {}


def _fetch_edgar_insider_sync(symbols: list[str], days_back: int = 30) -> dict:
    """
    Fetch Form 4 insider filing counts from SEC EDGAR for each symbol.
    Uses the official data.sec.gov submissions API (free, no key required).
    Falls back to yfinance insider_purchases on any per-symbol error.
    """
    import time
    import requests
    from datetime import datetime, timedelta

    cutoff = datetime.today() - timedelta(days=days_back)
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
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
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

            # Form 4 filings indicate insider transactions — we report the count
            # since the submissions API doesn't break down buy vs sell in metadata.
            # Any recent Form 4 activity is a useful signal for Claude.
            result[symbol] = {"recent_buys": form4_count, "recent_sells": 0}
        except Exception:
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
                result[symbol] = {"recent_buys": 0, "recent_sells": 0}
    return result


def _fetch_polygon_news_sync(symbols: list[str], api_key: str, limit_per_symbol: int = 5) -> dict:
    """
    Fetch recent news from Polygon.io for each symbol.
    Returns {symbol: [{"title": ..., "description": ...}, ...]}
    On free tier, description may be empty. On paid tier, it contains article summary.
    yfinance news remains as fallback in build_research_context().
    """
    import requests
    if not api_key:
        return {}
    result = {}
    base = "https://api.polygon.io/v2/reference/news"
    for symbol in symbols:
        try:
            resp = requests.get(
                base,
                params={"ticker": symbol, "limit": limit_per_symbol, "apiKey": api_key},
                timeout=10,
            )
            resp.raise_for_status()
            articles = resp.json().get("results", [])
            result[symbol] = [
                {
                    "title": a.get("title", ""),
                    "description": a.get("description", ""),
                }
                for a in articles
                if a.get("title")
            ]
        except Exception:
            result[symbol] = []
    return result


def _fetch_av_technicals_sync(symbols: list[str], api_key: str) -> dict:
    """
    Fetch RSI(14) from Alpha Vantage for each symbol.
    Returns {symbol: {"rsi": float, "signal": "overbought"|"oversold"|"neutral"}}
    Should only be called for screener picks (≤20 symbols) to stay within 25 calls/day free tier.
    Includes rate limiting (1.2s between calls) to avoid hitting the 5 calls/min free-tier limit.
    """
    import time
    import requests
    if not api_key or not symbols:
        return {}
    result = {}
    base = "https://www.alphavantage.co/query"
    for symbol in symbols:
        try:
            resp = requests.get(
                base,
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
            resp.raise_for_status()
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
            pass
        # Rate limit: AV free tier allows 5 calls/min; 1.2s spacing keeps us safe
        time.sleep(1.2)
    return result


def _fetch_options_data_sync(symbols: list[str]) -> dict:
    """Return put/call ratio, IV rank proxy, and 30-day implied move for each symbol."""
    result = {}
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            expirations = ticker.options
            if not expirations:
                result[symbol] = None
                continue
            # Use the nearest expiration that is at least 7 days out
            current_price = ticker.history(period="1d")["Close"].iloc[-1]
            target_exp = expirations[0]
            for exp in expirations:
                exp_date = date.fromisoformat(exp)
                if (exp_date - date.today()).days >= 7:
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
            result[symbol] = None
    return result


def _fetch_fred_macro_sync(api_key: str) -> dict:
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
                data = fred.get_series_latest_release(series_id)
                if data is not None and len(data) >= 2:
                    latest = float(data.iloc[-1])
                    prev = float(data.iloc[-2])
                    result[label] = {"value": round(latest, 3), "prev": round(prev, 3), "change": round(latest - prev, 3)}
                    if label == "cpi_index":
                        cpi_series_cache = data
            except Exception:
                pass
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
                pass
            # Remove the raw index entry — Claude only needs the YoY rate
            result.pop("cpi_index", None)
        # Compute yield curve spread
        if "treasury_10y" in result and "treasury_2y" in result:
            spread = result["treasury_10y"]["value"] - result["treasury_2y"]["value"]
            result["yield_curve_spread_10y2y"] = round(spread, 3)
        return result
    except Exception:
        return {}


def _fetch_market_context_sync(today: date, symbols: list[str] | None = None) -> str:
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
            pass
    sectors = {
        "XLK": "Tech", "XLF": "Financials", "XLE": "Energy",
        "XLV": "Healthcare", "XLI": "Industrials",
    }
    lines.append("\nSector performance (1-week):")
    for ticker_sym, label in sectors.items():
        try:
            t = yf.Ticker(ticker_sym)
            hist = t.history(period="5d")
            if hist.empty:
                continue
            price = float(hist["Close"].iloc[-1])
            week_ago = float(hist["Close"].iloc[0])
            chg = round((price - week_ago) / week_ago * 100, 2) if week_ago else 0
            lines.append(f"  {label} ({ticker_sym}): {chg:+.2f}%")
        except Exception:
            pass
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
                except Exception:
                    pass
        except Exception:
            pass
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
        pass
    return "\n".join(lines)


# S&P 500 members not already in WATCHLIST — used as the screener candidate pool.
# Update periodically as index composition changes. Excludes all 40 WATCHLIST symbols.
_SP500_POOL = [
    "A", "AAL", "AAP", "AbbVie", "ABC", "ACGL", "ACN", "ADSK", "AEE", "AEP",
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


def _fetch_momentum_screener_sync(n: int = 20) -> list[str]:
    """
    Return up to n symbols from the S&P 500 pool that are:
      - Not already in WATCHLIST
      - Price > 20-day moving average
      - Average daily volume > 1M shares
      - Ranked by 5-day price momentum (top n)
    Uses a hardcoded S&P 500 pool to avoid external HTTP calls for index composition.
    Falls back to [] on any error.
    """
    candidates = []
    for symbol in _SP500_POOL:
        if symbol in WATCHLIST:
            continue
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="3mo", interval="1d")
            if hist.empty or len(hist) < 25:
                continue
            current = float(hist["Close"].iloc[-1])
            ma20 = float(hist["Close"].tail(20).mean())
            avg_vol = float(hist["Volume"].tail(20).mean())
            momentum_5d = (current - float(hist["Close"].iloc[-6])) / float(hist["Close"].iloc[-6]) * 100
            if current > ma20 and avg_vol > 1_000_000:
                candidates.append((symbol, momentum_5d))
        except Exception:
            continue

    candidates.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _ in candidates[:n]]


def _fetch_opening_prices_sync(symbols: list[str], trade_date: date) -> dict[str, float | None]:
    results: dict[str, float | None] = {}
    date_str = trade_date.isoformat()
    next_day = (trade_date + timedelta(days=1)).isoformat()
    for symbol in symbols:
        try:
            df = yf.download(
                symbol,
                start=date_str,
                end=next_day,
                interval="1m",
                progress=False,
                auto_adjust=True,
            )
            if df.empty:
                results[symbol] = None
                continue
            # yfinance ≥0.2 returns MultiIndex columns even for a single symbol,
            # so df["Open"].iloc[0] is a Series not a scalar.
            # .values.flat[0] extracts the first scalar from a 1D or 2D array.
            results[symbol] = round(float(df["Open"].values.flat[0]), 4)
        except Exception:
            results[symbol] = None
    return results


_FRED_EXTRA_LABELS = {
    "retail_sales":     "Retail Sales",
    "credit_spread_hy": "HY Credit Spread (OAS)",
    "consumer_conf":    "Consumer Sentiment (UMich)",
    "pce_core":         "Core PCE",
    "industrial_prod":  "Industrial Production",
}


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
) -> str:
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

    # Per-stock data
    lines.append("=== WATCHLIST DATA ===")
    for symbol, data in sorted(price_data.items()):
        is_held = symbol in current_symbols
        held_marker = " [HELD]" if is_held else ""
        lines.append(f"\n{symbol}{held_marker}:")
        lines.append(f"  Price: ${data['current_price']:.2f} | 1wk: {data['week_change_pct']:+.1f}% | 1mo: {data['month_change_pct']:+.1f}%")
        lines.append(f"  52w range: ${data['low_52w']:.2f} – ${data['high_52w']:.2f}")
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
            if ia["recent_buys"] > 0 or ia["recent_sells"] > 0:
                lines.append(f"  Insider: {ia['recent_buys']:,} shares bought, {ia['recent_sells']:,} shares sold (recent)")

        # RSI from Alpha Vantage (screener picks only)
        if av_technicals and symbol in av_technicals:
            t = av_technicals[symbol]
            lines.append(f"  RSI(14): {t['rsi']} [{t['signal'].upper()}]")

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
                        lines.append(f"    - {title}: {desc[:150]}")
                    else:
                        lines.append(f"    - {title}")
                else:
                    lines.append(f"    - {a}")
        elif yf_headlines:
            lines.append("  News:")
            for h in yf_headlines[:3]:
                lines.append(f"    - {h}")

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


# ── Async wrappers ──────────────────────────────────────────────────────────

async def fetch_price_data(symbols: list[str]) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_price_data_sync, symbols)


async def fetch_news(symbols: list[str]) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_news_sync, symbols)


async def fetch_earnings_surprise(symbols: list[str]) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_earnings_surprise_sync, symbols)


async def fetch_insider_activity(symbols: list[str]) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_insider_activity_sync, symbols)


async def fetch_options_data(symbols: list[str]) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_options_data_sync, symbols)


async def fetch_fred_macro(api_key: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_fred_macro_sync, api_key)


async def fetch_market_context(today: date, symbols: list[str] | None = None) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_market_context_sync, today, symbols)


async def fetch_edgar_insider(symbols: list[str], days_back: int = 30) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_edgar_insider_sync, symbols, days_back)


async def fetch_polygon_news(symbols: list[str], api_key: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_polygon_news_sync, symbols, api_key)


async def fetch_av_technicals(symbols: list[str], api_key: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_av_technicals_sync, symbols, api_key)


async def fetch_momentum_screener(n: int = 20) -> list[str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_momentum_screener_sync, n)


async def fetch_opening_prices(symbols: list[str], trade_date: date) -> dict[str, float | None]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_opening_prices_sync, symbols, trade_date)


def _fetch_market_eod_sync(target_date: date) -> dict:
    """Fetch end-of-day performance for major indices and S&P sector ETFs."""
    import yfinance as yf

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

    def _pct(symbol: str) -> tuple[float | None, float | None]:
        try:
            hist = yf.Ticker(symbol).history(period="2d", interval="1d")
            if len(hist) < 2:
                return None, None
            close = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            return round(close, 2), round((close - prev) / prev * 100, 2)
        except Exception:
            return None, None

    result: dict = {"indices": {}, "sectors": {}}

    for label, symbol in indices.items():
        price, pct = _pct(symbol)
        if price is not None:
            result["indices"][label] = {"symbol": symbol, "price": price, "change_pct": pct}

    for symbol, label in sectors.items():
        _, pct = _pct(symbol)
        if pct is not None:
            result["sectors"][symbol] = {"label": label, "change_pct": pct}

    return result


async def fetch_market_eod(target_date: date) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_market_eod_sync, target_date)
