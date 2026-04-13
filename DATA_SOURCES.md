# Scorched — Data Sources & API Key Setup

This doc explains every data source the bot uses, which ones require free API keys, where to get them, and how to wire them up.

> **April 2026 update:** Polygon.io was removed. Alpaca Data API now provides prices, bars, snapshots, news, and the screener — all on the free IEX tier. yfinance is retained only for things Alpaca doesn't cover (fundamentals, options, earnings dates, insider data, index symbols like `^GSPC`/`^VIX`).

---

## Data Sources at a Glance

| Source | What it provides | API Key Required? | Free Tier? |
|--------|-----------------|:-----------------:|:----------:|
| **Alpaca Data API** | Price bars (IEX), snapshots, news headlines + summaries, screener (most active/movers) | Yes (shared with broker) | Yes (free IEX feed) |
| **yfinance** | Fundamentals (PE, market cap, short ratio), options chains, earnings dates, insider purchases, index symbols | No | Yes |
| **FRED** | Macro (Fed rate, yields, CPI, unemployment, PCE, credit spreads) + economic calendar releases | Yes | Yes |
| **Twelvedata** | RSI(14) for the full research watchlist | Yes | Yes (800 calls/day) |
| **Alpha Vantage** | RSI(14) for screener picks (fallback when Twelvedata is missing) | Yes | Yes (25 calls/day) |
| **Finnhub** | Analyst consensus ratings + price targets | Yes | Yes |
| **SEC EDGAR** | Form 4 insider filings | No | Yes |
| **Momentum screener** | Top S&P 500 movers (internal — built on Alpaca bars) | No | n/a |

---

## Keys You Need

### 1. Alpaca — Prices, News, Screener (and broker if you want live trading)

**What it's for:** Daily price bars, real-time snapshots, news headlines, and the "most active" screener — all from one API. Same key powers the broker if you opt into Alpaca paper or live trading.

**Why it matters:** This is the bot's primary market data source. Without it, Phase 0 has no prices, no news, and no screener.

**Get a free key:**
1. Sign up at [alpaca.markets](https://alpaca.markets) (free, no funding required for paper)
2. Go to **Paper Trading → API Keys → Generate New Key**
3. Save both the **API Key** and **Secret Key**

The free IEX data feed is sufficient for everything the bot needs. SIP feed is a paid $9/month upgrade — not required.

**Add to `.env`:**
```
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
```

---

### 2. FRED — Federal Reserve Economic Data

**What it's for:** Macro context — Fed funds rate, 10Y/2Y yield curve, CPI, unemployment, retail sales, HY credit spreads, PCE, industrial production. Also drives the **economic calendar** (upcoming CPI/Jobs/FOMC/GDP releases).

**Why it matters:** Without it, the bot skips all macro signals and runs blind on the macro regime.

**Get a free key:**
1. Go to [https://fred.stlouisfed.org/docs/api/api_key.html](https://fred.stlouisfed.org/docs/api/api_key.html)
2. Create a free account
3. Request an API key — approved instantly

**Add to `.env`:**
```
FRED_API_KEY=your_key_here
```

---

### 3. Twelvedata — RSI for the Full Watchlist

**What it's for:** RSI(14) for every symbol in the research universe (~80 stocks). Twelvedata's free tier offers 800 API calls/day, more than enough for daily coverage.

**Why it matters:** Without it, RSI falls back to Alpha Vantage which is limited to ~20 symbols/day.

**Get a free key:**
1. Sign up at [twelvedata.com](https://twelvedata.com)
2. Get your API key from **Account → API Keys**

**Add to `.env`:**
```
TWELVEDATA_API_KEY=your_key_here
```

---

### 4. Alpha Vantage — RSI Fallback

**What it's for:** RSI(14) for the momentum screener picks only (~20 symbols/day). Used as a fallback when Twelvedata is unavailable.

**Why it matters:** Optional. The free tier is 25 calls/day — enough since it's only used for screener picks.

**Get a free key:**
1. Go to [https://www.alphavantage.co/support/#api-key](https://www.alphavantage.co/support/#api-key)
2. Enter your email and get a key instantly — no account required

**Add to `.env`:**
```
ALPHA_VANTAGE_API_KEY=your_key_here
```

---

### 5. Finnhub — Analyst Consensus

**What it's for:** Buy/hold/sell counts and recommendation trend changes for candidate stocks.

**Why it matters:** Optional but recommended. Adds Wall Street consensus to the analyst's view.

**Get a free key:**
1. Sign up at [finnhub.io](https://finnhub.io)
2. Get the API key from your dashboard

**Add to `.env`:**
```
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
- All optional keys degrade gracefully — the bot runs without them, just with less data.
- Never commit your `.env` file to git. It's in `.gitignore` by default.
- If you're deploying to a remote server, set the environment variables there directly rather than copying `.env` — especially the `ANTHROPIC_API_KEY` and `ALPACA_SECRET_KEY`.
