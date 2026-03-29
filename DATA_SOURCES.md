# Scorched — Data Sources & API Key Setup

This doc explains every data source the bot uses, which ones require free API keys, where to get them, and how to wire them up.

---

## Data Sources at a Glance

| Source | What it provides | API Key Required? | Free Tier? |
|--------|-----------------|:-----------------:|:----------:|
| **yfinance** | Price history, fundamentals, news, earnings, options, insider purchases | No | Yes |
| **FRED** | Macro indicators (Fed rate, yields, CPI, unemployment, PCE, etc.) | Yes | Yes |
| **Polygon.io** | Higher-quality news headlines | Yes | Yes (limited) |
| **Alpha Vantage** | RSI(14) momentum signals | Yes | Yes (25 calls/day) |
| **SEC EDGAR** | Form 4 insider buy/sell filings | No | Yes |
| **Momentum screener** | Top S&P 500 movers (internal, no external API) | No | Yes |

---

## Keys You Need

### 1. FRED — Federal Reserve Economic Data
**What it's for:** Macro context — Fed funds rate, 10Y/2Y yield curve, CPI, unemployment, retail sales, HY credit spreads, PCE, industrial production.

**Why it matters:** The bot skips all macro data if this key is missing. Strongly recommended.

**Get a free key:**
1. Go to [https://fred.stlouisfed.org/docs/api/api_key.html](https://fred.stlouisfed.org/docs/api/api_key.html)
2. Create a free account
3. Request an API key — approved instantly

**Add to `.env`:**
```
FRED_API_KEY=your_key_here
```

---

### 2. Polygon.io — Market News
**What it's for:** Better news headlines than yfinance for the stocks the bot is analyzing. Used to surface relevant recent news before Claude makes a decision.

**Why it matters:** Optional but improves news quality. If missing, the bot falls back to yfinance news.

**Get a free key:**
1. Go to [https://polygon.io](https://polygon.io) and create a free account
2. Your API key is on the dashboard under **API Keys**
3. The free "Starter" plan is sufficient

**Add to `.env`:**
```
POLYGON_API_KEY=your_key_here
```

---

### 3. Alpha Vantage — RSI Signals
**What it's for:** RSI(14) values for momentum screener candidates (top ~20 stocks being evaluated). Used to filter out overbought stocks.

**Why it matters:** Optional. If missing, RSI data is skipped. Free tier is 25 API calls/day — enough since the bot only checks ~20 symbols daily.

**Get a free key:**
1. Go to [https://www.alphavantage.co/support/#api-key](https://www.alphavantage.co/support/#api-key)
2. Enter your email and get a key instantly — no account required

**Add to `.env`:**
```
ALPHA_VANTAGE_API_KEY=your_key_here
```

---

## Full `.env` File Template

```env
# Required
ANTHROPIC_API_KEY=sk-ant-api03-...
STARTING_CAPITAL=100000

# Recommended — macro data (FRED)
FRED_API_KEY=

# Optional — better news headlines
POLYGON_API_KEY=

# Optional — RSI signals for screener
ALPHA_VANTAGE_API_KEY=

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
1. Walk me through getting a FRED API key at fred.stlouisfed.org (free, instant)
2. Walk me through getting a Polygon.io API key (free Starter plan)
3. Walk me through getting an Alpha Vantage API key (free, no account needed)
4. Once I have the keys, create a .env file in the project root using the
   template in DATA_SOURCES.md, filling in the keys I provide

I already have my ANTHROPIC_API_KEY. Let's start with FRED.
```

Claude Code will guide you through each step, ask for the keys as you get them, and write the `.env` file for you.

---

## Notes

- **yfinance** and **SEC EDGAR** are completely free with no registration. They work out of the box.
- All optional keys degrade gracefully — the bot runs without them, just with less data.
- Never commit your `.env` file to git. It's in `.gitignore` by default.
- If you're deploying to a remote server, set the environment variables there directly rather than copying `.env` — especially the `ANTHROPIC_API_KEY`.
