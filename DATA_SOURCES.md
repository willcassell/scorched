# Scorched — Data Sources & API Key Setup

This doc explains every data source the bot pulls from, what specific data each one contributes, how the bot actually uses it, and where the limits are. The goal is to give you enough detail to **judge how robust the inputs are** before you trust the output.

> **April 2026 update:** Polygon.io was removed. Alpaca Data API now provides prices, bars, snapshots, news, and the screener — all on the free IEX tier. yfinance is retained only for things Alpaca doesn't cover (fundamentals, options, earnings dates, insider data, index symbols like `^GSPC`/`^VIX`).

---

## Data Sources at a Glance

| Source | What it provides | API Key? | Free Tier | Robustness |
|--------|------------------|:--------:|-----------|------------|
| **Alpaca Data API** | Daily price bars (1-yr), live snapshots, news headlines + summaries, "most active" screener | Yes (shared with broker) | Yes — IEX feed | Official broker API, stable. IEX feed is ~3% of consolidated volume; price ticks may lag SIP slightly. |
| **yfinance** | Fundamentals (PE, market cap, short interest), options chains, earnings dates, insider purchases, index symbols (`^GSPC`, `^VIX`) | No | Yes | Unofficial scraping of Yahoo Finance. Works reliably in practice but can break without notice when Yahoo changes their site. |
| **FRED** | Macro indicators (Fed funds, yields, CPI, PCE, unemployment, credit spreads) + economic calendar | Yes | Yes (unlimited) | Authoritative source — published by the St. Louis Fed. No rate limits in practice. |
| **Twelvedata** | RSI(14) for the full ~80-symbol research watchlist | Yes | 800 calls/day | Established financial data provider; quality is consistent. Free tier is more than enough for daily use. |
| **Alpha Vantage** | RSI(14) fallback for screener picks (~20 symbols) | Yes | 25 calls/day | Reliable but tight free-tier limit; only used as a backstop. |
| **Finnhub** | Analyst recommendations (buy/hold/sell counts), recommendation trend changes, price targets | Yes | Yes | Aggregates Wall Street analyst data. Free tier is sufficient for daily polling. |
| **SEC EDGAR** | Form 4 insider transaction filing counts (last 60 days) | No | Yes | Authoritative — direct from the SEC. Filing counts only (the API doesn't reliably distinguish buys from sells on the free path). |
| **Internal momentum screener** | Top 20 S&P 500 movers by 5-day momentum (price > 20d MA, avg vol > 1M) | No | n/a | Built on Alpaca bars — accuracy is whatever Alpaca's data is. |

---

## What Each Source Actually Contributes

### 1. Alpaca Data API — the backbone of everything per-stock

This is the single most important source. If Alpaca is down or unkeyed, the bot has almost no per-stock data.

**What you get:**
- **Daily price bars** for every watchlist symbol (1-year history, OHLCV) — used to compute moving averages, MACD, Bollinger Bands, support/resistance, ATR, and the relative-strength-vs-sector calculation.
- **Live snapshots** at Phase 0 (9:35 AM ET, 5 min after the open): latest trade price, previous daily close, today's open/high/low/volume. This is how the bot detects overnight gaps and opening-range volatility.
- **News headlines + summaries** for every candidate symbol — fed into Claude's analysis as the catalyst signal. ("Why is this stock moving today?")
- **Screener** (`MostActives`) — supplements the static watchlist with whatever the broader market is trading on a given day.

**How the bot uses it in decisions:**
- "Is the stock up 3–8% over 5 days?" — bars
- "Did it gap down >2% overnight?" — snapshots (used by the circuit breaker to block stale recommendations)
- "Is there a fresh, named catalyst?" — news (no catalyst = no buy, per the hard rules in `analyst_guidance.md`)
- "Is volume confirming the move?" — snapshots vs. 20-day average from bars
- "What sectors are leading today?" — bar data for 11 sector ETFs (XLK, XLF, XLE, etc.)

**Limits to know:**
- Free IEX feed represents ~3% of total US equity volume. For the daily/swing horizon this bot trades on, that's fine — you're looking at trends, not microsecond ticks. SIP feed ($9/mo) gives full consolidated tape but isn't required.
- Alpaca news is U.S.-equity focused and skews toward press-release wires; you won't see deep-dive financial journalism here.
- Index symbols (`^GSPC`, `^VIX`, `^IXIC`) are *not* in Alpaca — those come from yfinance.

---

### 2. yfinance — fundamentals, options, and everything Alpaca doesn't cover

Used as a complement to Alpaca, not a replacement. No API key needed.

**What you get:**
- **Fundamentals:** trailing PE, forward PE, market cap, short ratio, short percent of float — used to flag overvalued / heavily-shorted setups.
- **Options data** (candidates only): put/call ratio, ATM implied volatility, implied 30-day move — fed into Claude as a sentiment + risk gauge.
- **Earnings dates** — used to enforce the "no new position within 3 trading days of earnings" hard rule.
- **Insider purchases** (recent 6-month window via Yahoo's insider page) — secondary signal alongside SEC EDGAR.
- **Index symbols** (`^GSPC` for SPY, `^VIX`, `^IXIC` for Nasdaq, `^DJI`) — used by the circuit breaker and macro context.

**How the bot uses it in decisions:**
- "Is implied 30-day move smaller than my thesis requires?" — flagged in `key_risks`
- "Is earnings within 3 days?" — automatic rejection via hard rule
- "Is short interest >10% with a positive catalyst?" — flagged as a potential squeeze setup
- "Is VIX above 30?" — circuit breaker blocks all new buys

**Limits to know:**
- yfinance is **unofficial** — it scrapes Yahoo Finance. Yahoo can (and occasionally does) change their site and break the library. The bot logs warnings when this happens but can't recover automatically.
- All yfinance calls are synchronous and slow; the code wraps them in a thread executor so they don't block.
- Insider data is "recent purchases" only and lags Form 4 filings by a day or two.

---

### 3. FRED — the macro lens

FRED (Federal Reserve Economic Data) gives the bot a view of the broader economic environment, so it can size positions differently in a hiking-Fed/inverted-curve world vs. a cutting-Fed/risk-on world.

**What you get** (each pulled fresh in Phase 0):
- **Fed funds rate** (`DFF`) + trajectory (cutting / holding / hiking)
- **10-Year Treasury yield** (`DGS10`) and **2-Year** (`DGS2`) → yield curve spread (recession signal when deeply inverted)
- **CPI YoY** (`CPIAUCSL`) — inflation regime
- **Core PCE** (`PCEPILFE`) — Fed's preferred inflation gauge
- **Unemployment rate** (`UNRATE`)
- **High-yield credit spread / OAS** (`BAMLH0A0HYM2`) — risk-on/risk-off indicator (>600bps = risk-off)
- **Consumer sentiment** (`UMCSENT`)
- **Industrial production** (`INDPRO`), **retail sales** (`RSXFS`)
- **Economic calendar** (FRED releases API): upcoming dates for CPI, Jobs report, FOMC, GDP, PPI, PCE — Claude is warned if a major release is same-day.

**How the bot uses it in decisions:**
- The "Macro Read" section of every analysis is built directly from these series, with a 7-row bull/neutral/bear table (see `analyst_guidance.md`).
- Hard rule: if 3+ macro indicators are bearish, new position sizes get cut at least in half.
- Hard rule: if SPY is down >2% today *or* VIX >30, no new buys at all.
- Economic calendar: if CPI is releasing today and you're considering a buy, Claude is told to be more cautious.

**Limits to know:**
- FRED data is high-quality but **not real-time** — most series are released monthly with a lag. CPI for March, for example, is published mid-April. The bot uses the most recently published value.
- Free tier has no published rate limit, but excessive polling (>120 calls/min) will throttle.

---

### 4. Twelvedata — RSI(14) for the whole watchlist

RSI (Relative Strength Index) is the bot's primary overbought/oversold signal. Twelvedata covers the full universe so every candidate has an RSI value.

**What you get:** 14-day RSI for ~80 symbols, refreshed daily in Phase 0.

**How the bot uses it:**
- **40–65:** healthy momentum range, fine to enter
- **65–70:** approaching overbought, lower confidence
- **>70:** overbought — requires an exceptional catalyst, noted in `key_risks`
- **<30:** oversold — wrong direction for a momentum strategy, avoid

**Limits to know:**
- Free tier is **800 API calls/day** (~10× the bot's needs).
- Tied to a single 5-second polling window; the bot serializes RSI requests to stay within rate limits.
- If Twelvedata is down, the bot falls back to Alpha Vantage but only covers the ~20 screener picks.

---

### 5. Alpha Vantage — RSI fallback for screener picks

A backstop for RSI when Twelvedata isn't available, scoped to the smaller screener universe so it fits the tight free-tier budget.

**What you get:** 14-day RSI for ~20 momentum-screener symbols.

**How the bot uses it:** Same RSI interpretation as Twelvedata above, just narrower coverage.

**Limits to know:**
- **25 calls/day free tier** — extremely tight. The bot only calls it for screener picks (≤20 symbols) so it stays within budget.
- If you have Twelvedata configured, Alpha Vantage is essentially unused.

---

### 6. Finnhub — Wall Street consensus

Adds the "what does the Street think?" dimension to Claude's analysis — a useful sanity check against being wildly off-consensus.

**What you get** (per candidate):
- **Recommendation breakdown:** number of analysts at strong buy / buy / hold / sell / strong sell
- **Recommendation trend:** how those numbers changed this month vs. last month (helps spot upgrades / downgrades in motion)
- **Price target:** mean / high / low — the bot computes the gap between current price and mean target

**How the bot uses it in decisions:**
- ">80% buy/strong-buy" → bullish but watch for crowded trade risk
- "Price already above mean target" → easy upside is gone, requires a re-rating catalyst
- "Mean target >20% above current" → meaningful upside if thesis plays out
- These get summarized into the per-candidate context Claude sees.

**Limits to know:**
- Free tier covers individual symbol queries fine; congressional trading data was removed in April 2026 (paid tier only).
- Finnhub aggregates analyst ratings but doesn't include the underlying research notes — you only see the score, not the reasoning.

---

### 7. SEC EDGAR — insider activity

Insider buys and sells from official Form 4 filings. Free, no key, direct from the regulator.

**What you get:** count of Form 4 filings per symbol over the last ~60 days.

**How the bot uses it:**
- Fed into the per-symbol "scoring" used to rank which candidates make it into Claude's top 25.
- Surfaced in Claude's analysis context as "X recent Form 4 filings (type unknown)."

**Limits to know:**
- The free EDGAR endpoint returns **filing counts**, not reliable buy-vs-sell classification. The bot intentionally avoids claiming "X insider buys" — that requires parsing each filing's `transactionCode`, which is brittle.
- For high-conviction insider buy clusters, you'd need a paid feed (e.g., InsiderScore, OpenInsider).

---

### 8. Internal Momentum Screener — candidate sourcing

Not an external API — this is logic in `services/research.py` that scans the S&P 500 using Alpaca bars.

**What it gives you:** top 20 symbols by 5-day price momentum, after filtering for:
- Price above 20-day moving average (uptrend confirmation)
- Average volume > 1M shares/day (liquidity threshold)

**How it's used:** Adds 20 fresh symbols to the daily research universe each morning, on top of the static watchlist and current holdings. Without the screener, the bot would only ever consider a fixed list.

**Limits to know:**
- Quality is bounded by Alpaca's data quality (so: very good, but constrained to IEX feed).
- The 5-day window captures recent momentum but can miss longer-base breakouts.

---

## How These Sources Combine

A typical Phase 0 morning fetch pulls all eight sources in parallel. The bot then:

1. Builds a per-symbol "scorecard" — momentum, news catalyst, technicals, volume, relative strength, insider activity.
2. Ranks symbols by score and keeps the top 25 (plus all currently-held positions).
3. Hands that compressed context to Claude with the macro table, options data, and Wall Street consensus attached per candidate.

This is why a missing data source matters: if Twelvedata is down, RSI drops out of the scorecard for most symbols. If FRED is down, the macro table is empty and Claude is told "macro signals unavailable." The bot doesn't crash — it just makes decisions with less information.

---

## Setup — Get the Keys

### 1. Alpaca — Prices, News, Screener (and broker if you want live trading)

The single most important key. Same key powers data + paper/live trading.

1. Sign up at [alpaca.markets](https://alpaca.markets) (free, no funding required)
2. Go to **Paper Trading → API Keys → Generate New Key**
3. Save both the **API Key** and **Secret Key**

```env
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
```

The free IEX data feed is sufficient for everything the bot does. SIP feed ($9/mo) is not required.

---

### 2. FRED — Federal Reserve Economic Data

1. Go to [fred.stlouisfed.org/docs/api/api_key.html](https://fred.stlouisfed.org/docs/api/api_key.html)
2. Create a free account and request an API key (instant approval)

```env
FRED_API_KEY=your_key_here
```

---

### 3. Twelvedata — RSI for the Full Watchlist

1. Sign up at [twelvedata.com](https://twelvedata.com)
2. Get the API key from **Account → API Keys**

```env
TWELVEDATA_API_KEY=your_key_here
```

---

### 4. Alpha Vantage — RSI Fallback

1. Go to [alphavantage.co/support/#api-key](https://www.alphavantage.co/support/#api-key)
2. Enter your email — instant key, no account required

```env
ALPHA_VANTAGE_API_KEY=your_key_here
```

---

### 5. Finnhub — Analyst Consensus

1. Sign up at [finnhub.io](https://finnhub.io)
2. Grab the API key from your dashboard

```env
FINNHUB_API_KEY=your_key_here
```

---

## Full `.env` File Template

```env
# Required
ANTHROPIC_API_KEY=sk-ant-api03-...
STARTING_CAPITAL=100000

# Strongly recommended — primary market data (Alpaca)
ALPACA_API_KEY=
ALPACA_SECRET_KEY=

# Optional broker mode (default: paper). Set to alpaca_paper or alpaca_live to route orders to Alpaca.
# BROKER_MODE=paper

# Strongly recommended — macro data (FRED)
FRED_API_KEY=

# Recommended — RSI for full watchlist
TWELVEDATA_API_KEY=

# Optional — RSI fallback for screener picks
ALPHA_VANTAGE_API_KEY=

# Optional — analyst consensus
FINNHUB_API_KEY=

# Only needed for local runs outside Docker
# DATABASE_URL=postgresql+asyncpg://scorched:scorched@localhost:5432/scorched

# Optional — require a PIN to change strategy via the dashboard
# SETTINGS_PIN=
```

---

## What This Setup Doesn't Give You

Being honest about limits so you can judge robustness:

- **No real-time order flow, dark pool, or DEX data** — everything here is public market data.
- **No social sentiment** (Reddit, Twitter, StockTwits). The bot reads news; it doesn't read forums.
- **No fundamental research notes** — Finnhub gives you the analyst rating, not the analyst's reasoning.
- **No alternative data** (credit-card panels, satellite imagery, web traffic).
- **News coverage is U.S. equity-focused** and skews toward press-release wires. Specialized industry coverage (e.g., biotech-specific outlets) is not included.
- **Insider data is filing counts, not parsed buy/sell classification** — see EDGAR section above.

For a short-horizon momentum strategy on liquid large caps, this set is sufficient. If you wanted to trade small-caps, biotech catalysts, or crypto, you'd need additional sources.

---

## Let Claude Code Set This Up For You

You can paste the following prompt directly into Claude Code and it will handle the setup:

```
I'm setting up the Scorched trading bot. Please help me configure the required
API keys and environment file.

Here's what I need:
1. Walk me through getting an Alpaca API key (free paper account, no funding)
2. Walk me through getting a FRED API key at fred.stlouisfed.org (free, instant)
3. Walk me through getting a Twelvedata API key (free, 800 calls/day)
4. Walk me through getting a Finnhub API key (free)
5. Once I have the keys, create a .env file in the project root using the
   template in DATA_SOURCES.md, filling in the keys I provide

I already have my ANTHROPIC_API_KEY. Let's start with Alpaca.
```

Claude Code will guide you through each step, ask for the keys as you get them, and write the `.env` file for you.

---

## Notes

- **yfinance** and **SEC EDGAR** are completely free with no registration. They work out of the box.
- All optional keys degrade gracefully — the bot runs without them, just with less data. A startup log warning tells you which sources are missing.
- Never commit your `.env` file to git. It's in `.gitignore` by default.
- If you're deploying to a remote server, set the environment variables there directly rather than copying `.env` — especially the `ANTHROPIC_API_KEY` and `ALPACA_SECRET_KEY`.
- The dashboard's `/system` page shows live red/yellow/green status for each source, and the API call log persists for 30 days so you can spot a flaky source over time.
