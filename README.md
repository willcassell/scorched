# Scorched — Your AI Trading Assistant

An AI-powered stock trading bot you can actually talk to. Every weekday, Scorched researches the market, generates trade recommendations through a multi-stage Claude pipeline, and manages a portfolio — all on its own. Check in through a live dashboard, or **open Claude Desktop and ask your bot what it's thinking in plain English.**

Starts as paper trading with simulated money. When you're ready, connect an Alpaca brokerage account to trade for real.

## New here? Start with [START_HERE.md](START_HERE.md)

---

## Two Ways to Use It

### 1. Talk to Your Bot (MCP)

Scorched runs a built-in **MCP server** — the open protocol that lets AI assistants use external tools. Point Claude Desktop (or any MCP client) at your bot and have a conversation:

> **You:** "What did you buy today and why?"
> **You:** "How's the portfolio doing?"
> **You:** "What does your playbook say about tech stocks?"
> **You:** "Run today's analysis — what looks good?"

Claude calls the bot's tools behind the scenes and answers in plain English. No commands to memorize, no API knowledge needed. **This is the easiest way to get started.**

**Quick setup (Claude Desktop):**
```json
{
  "mcpServers": {
    "tradebot": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

That's it. Open Claude Desktop and start asking questions.

### 2. Set It and Forget It (Dashboard + Cron)

The bot also runs fully autonomously on a daily schedule — no manual intervention required. Cron jobs trigger each phase of the trading day, and a live dashboard at `http://host:8000` shows your portfolio, today's picks, and performance history. Auto-refreshes every 5 minutes.

```
Cron (VM)
    │
    │  7:30 AM ET — Data prefetch (zero LLM cost)
    │  8:30 AM ET — Claude analysis + recommendations
    │  9:30 AM ET — Circuit breaker safety gate
    │  9:35 AM ET — Execute approved trades
    │  Every 5 min — Intraday position monitoring
    │  4:01 PM ET — EOD review + playbook update
    │
    ▼
Scorched (FastAPI + PostgreSQL)
    │
    ├── Phase 0: Fetches market data (yfinance, FRED, Polygon, Twelvedata, Finnhub, EDGAR)
    ├── Phase 0: Runs momentum screener (top 20 S&P 500 movers)
    ├── Phase 1: Calls Claude (claude-sonnet-4-6) — multi-call pipeline
    │     Call 1: Analysis w/ extended thinking → identify candidates
    │     Call 2: Decision → 0–3 concrete trade recommendations
    │     Call 3: Risk committee → challenge and reject weak picks
    │     Call 4: Position management → review open positions EOD
    ├── Tracks portfolio state in PostgreSQL
    └── Dashboard auto-refreshes at http://host:8000
```

**NYSE holidays** are detected automatically — if the market is closed, no Claude calls are made.

---

## Stack

| Layer | Technology |
|-------|-----------|
| Runtime | Python 3.11, FastAPI |
| AI | Anthropic Claude (`claude-sonnet-4-6`, extended thinking on Call 1) |
| MCP | `mcp[cli]` (FastMCP, Streamable HTTP) — talk to your bot from Claude Desktop |
| Database | PostgreSQL 16 via SQLAlchemy 2.0 async + asyncpg |
| Migrations | Alembic |
| Market data | yfinance (prices, fundamentals, options, earnings, news, insider) |
| Macro data | FRED API (Fed rate, CPI, yield curve, PCE, credit spreads, economic calendar) |
| News | Polygon.io (preferred) + yfinance fallback |
| Technicals | Twelvedata RSI(14) for full watchlist + Alpha Vantage fallback |
| Analyst consensus | Finnhub (recommendation trends, congressional trading) |
| Insider filings | SEC EDGAR Form 4 (free, no key) |
| Holiday detection | `pandas-market-calendars` (NYSE calendar) |
| Automation | cron on the VM |
| Deployment | Docker Compose |

---

## MCP Server — Talk to Your Bot

The bot runs a **Streamable HTTP MCP server** at `http://host:8000/mcp`. Any MCP-compatible client — Claude Desktop, Cursor, your own agents — can connect and interact with the full trading system through natural language.

**Why this matters:**
- **No technical knowledge required.** Ask "what did you buy today?" and get a plain-English answer. The AI client calls the right tools automatically.
- **The heavy lifting is behind the tools.** A single question like "what looks good today?" triggers a multi-stage pipeline that pulls from 7+ data sources, runs technical analysis, and passes picks through a risk committee. You just ask.
- **Human-in-the-loop by default.** Recommendations come back as pending. Nothing executes without explicit confirmation.
- **Works with any MCP client.** Claude Desktop, Cursor, Claude Code, or any tool that speaks the MCP protocol.

**Connect Claude Desktop** — add this to your Claude Desktop MCP config:
```json
{
  "mcpServers": {
    "tradebot": {
      "url": "http://your-server:8000/mcp"
    }
  }
}
```

**Things you can ask:**
- *"How's my portfolio doing?"* — pulls live positions, P&L, and tax status
- *"Run today's analysis"* — triggers the full research + recommendation pipeline
- *"What does the playbook say?"* — reads the bot's evolving strategy document
- *"Show me the market summary"* — end-of-day index and sector performance
- *"Confirm trade #42 at $185.50 for 10 shares"* — executes a specific recommendation
- *"Reject recommendation #43"* — skips a pick while keeping the audit trail clean

**Available tools (7):**

| Tool | What it does |
|------|-------------|
| `get_recommendations` | Research stocks + generate up to 3 trade picks via Claude pipeline |
| `get_opening_prices` | Fetch actual opening auction prices for any symbols |
| `confirm_trade` | Execute a trade — updates portfolio, tracks P&L and taxes |
| `reject_recommendation` | Skip a pick (audit trail stays clean) |
| `get_portfolio` | Live portfolio snapshot — positions, unrealized P&L, tax classification |
| `get_market_summary` | EOD performance for major indices + all S&P 500 sector ETFs |
| `read_playbook` | Read the bot's living strategy doc — lessons learned from past trades |

---

## REST API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard (HTML) |
| `GET` | `/health` | Health check |
| `GET` | `/api/v1/portfolio` | Portfolio snapshot |
| `GET` | `/api/v1/portfolio/history` | Trade history (paginated) |
| `GET` | `/api/v1/portfolio/tax-summary` | YTD realized gains by ST/LT |
| `GET` | `/api/v1/recommendations` | List past sessions |
| `POST` | `/api/v1/recommendations/generate` | Trigger recommendation run |
| `POST` | `/api/v1/trades/confirm` | Confirm a trade |
| `POST` | `/api/v1/trades/{rec_id}/reject` | Reject a recommendation |
| `GET` | `/api/v1/market/summary` | EOD market + sector summary |
| `GET` | `/api/v1/strategy` | Read strategy settings |
| `PUT` | `/api/v1/strategy` | Update strategy settings (PIN-protected if configured) |
| `GET` | `/api/v1/playbook` | Read strategy playbook |
| `POST` | `/api/v1/intraday/evaluate` | Intraday trigger check + Claude exit evaluation |
| `GET` | `/api/v1/costs` | Claude API cost tracker |

---

## Database Schema

8 tables in PostgreSQL:

| Table | Purpose |
|-------|---------|
| `portfolio` | Single-row: cash balance, starting capital, peak value (drawdown tracking), benchmark start prices |
| `positions` | One row per held ticker; avg cost basis updated on each buy |
| `recommendation_sessions` | One row per trading day; caches raw research + Claude response + analysis thinking |
| `trade_recommendations` | Up to 3 rows per session; status: `pending` → `confirmed`/`rejected` |
| `trade_history` | Append-only audit log of executed trades |
| `playbook` | Single-row living strategy document (updated by Claude before each session) |
| `token_usage` | Per-call Claude API token tracking (input, output, thinking tokens) |
| `api_call_log` | External API call tracking — service, endpoint, status, response time, errors |

**Tax model:** simplified ST/LT classification based on `first_purchase_date` (no per-lot tracking). ST rate: 37%, LT rate: 20%.

---

## Project Structure

```
tradebot/
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh           # Runs alembic upgrade then starts uvicorn
├── pyproject.toml
├── strategy.md             # Human-readable strategy reference
├── analyst_guidance.md     # Signal interpretation tables + hard rules for Claude prompts
├── advisor.md              # CPA/financial advisor reference document
├── DEPLOY.md               # Full deployment + cron guide
├── alembic/
│   ├── env.py
│   └── versions/           # Migration files
├── cron/                   # Cron job scripts (phase 0, intraday monitor, etc.)
├── scripts/                # Utility scripts (setup_cron.py, etc.)
└── src/
    └── scorched/
        ├── main.py         # FastAPI app; mounts MCP at /mcp
        ├── config.py       # pydantic-settings Settings
        ├── database.py     # Async SQLAlchemy engine + session
        ├── models.py       # 8 ORM models
        ├── schemas.py      # Pydantic request/response schemas
        ├── mcp_tools.py    # 7 MCP tool definitions (FastMCP)
        ├── tax.py          # classify_gain(), estimate_tax()
        ├── cost.py         # Claude token cost calculator
        ├── tz.py           # market_today(), market_now(), MARKET_TZ
        ├── api_tracker.py  # External API call tracking + health aggregation
        ├── correlation.py  # 20-day return correlation check
        ├── circuit_breaker.py  # Pre-execution gate (gap-down, SPY, VIX)
        ├── drawdown_gate.py    # Portfolio drawdown enforcement
        ├── trailing_stops.py   # ATR-based trailing stop logic
        ├── intraday.py     # Pure intraday trigger check functions
        ├── http_retry.py   # Retry wrapper for external HTTP APIs
        ├── static/
        │   └── dashboard.html
        ├── broker/         # BrokerAdapter ABC, PaperBroker, AlpacaBroker
        ├── api/            # FastAPI routers
        │   ├── costs.py
        │   ├── market.py
        │   ├── playbook.py
        │   ├── portfolio.py
        │   ├── recommendations.py
        │   ├── strategy.py
        │   ├── trades.py
        │   ├── system.py       # /system/health, /system/errors, /system/trend
        │   ├── intraday.py     # Intraday trigger eval + auto-sell
        │   ├── prefetch.py     # Phase 0 data prefetch
        │   ├── onboarding.py
        │   └── broker_status.py  # Position reconciliation
        └── services/
            ├── portfolio.py      # apply_buy(), apply_sell(), get_portfolio_state()
            ├── recommender.py    # Claude 4-call pipeline + NYSE holiday check
            ├── research.py       # All data fetching (yfinance, FRED, Polygon, Twelvedata, Finnhub, EDGAR)
            ├── technicals.py     # MACD, Bollinger, MA crossover, support/resistance, ATR
            ├── finnhub_data.py   # Analyst consensus, price targets, congressional trading
            ├── economic_calendar.py  # FRED-based upcoming release tracking
            ├── risk_review.py    # Call 3: adversarial risk committee review
            ├── position_mgmt.py  # Call 4: EOD position management review
            ├── reflection.py     # Weekly trade reflection + learnings
            ├── playbook.py       # Playbook read/update
            └── strategy.py       # load_strategy() from strategy.json
```

---

## Quick Start (Local)

### Prerequisites
- Docker + Docker Compose
- An Anthropic API key

### 1. Clone and configure

```bash
git clone <repo>
cd tradebot
cp .env.example .env
# Edit .env — at minimum set ANTHROPIC_API_KEY
```

### 2. Start everything

```bash
docker compose up -d --build
```

This starts PostgreSQL, waits for it to be healthy, then starts the app (Alembic migrations run automatically at startup).

### 3. Verify

```bash
curl http://localhost:8000/health
# {"status":"ok","db":"connected"}

# Open dashboard
open http://localhost:8000
```

### 4. Trigger a recommendation run manually

```bash
curl -s -X POST http://localhost:8000/api/v1/recommendations/generate \
  -H "Content-Type: application/json" \
  -d '{}'
```

---

## Environment Variables

Create `.env` in the project root (see `.env.example` for full template):

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-api03-...

# Portfolio
STARTING_CAPITAL=100000          # Starting cash in dollars

# Tax rates (optional — these are the defaults)
SHORT_TERM_TAX_RATE=0.37
LONG_TERM_TAX_RATE=0.20

# Server
PORT=8000
HOST=0.0.0.0

# Optional data sources (enable richer context)
FRED_API_KEY=                    # Free: https://fredaccount.stlouisfed.org
ALPHA_VANTAGE_API_KEY=           # Free tier: 25 calls/day
POLYGON_API_KEY=                 # Free tier for news
FINNHUB_API_KEY=                 # Free: analyst consensus + congressional trading
TWELVEDATA_API_KEY=              # Free tier: 800 calls/day, RSI for full watchlist

# Optional: require a PIN to update strategy via dashboard
SETTINGS_PIN=

# Optional broker (default: paper trading, no broker needed)
BROKER_MODE=paper                # "paper", "alpaca_paper", or "alpaca_live"
ALPACA_API_KEY=
ALPACA_SECRET_KEY=

# Optional notifications
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

> **Note:** `DATABASE_URL` is not needed in `.env` when using Docker Compose — it's set automatically via the `environment` block in `docker-compose.yml`.

---

## Deployment (Oracle Cloud / Ubuntu VM)

See **[DEPLOY.md](DEPLOY.md)** for the full guide, including:
- rsync command to copy files to the VM
- `.env` setup on the VM
- Firewall / port configuration
- Cron job setup for the automated daily cycle
- DST time adjustments

**DST reminder:** The cron jobs use UTC times. When US clocks spring forward (around March 8), ET shifts from UTC-5 to UTC-4. Update the crontab:

```
Phase 1: 30 13 → 30 12   (8:30 AM ET)
Phase 2: 45 14 → 45 13   (9:45 AM ET)
```

---

## Dashboard

The web dashboard at `http://host:8000` shows:
- Portfolio performance and today's picks
- Open positions with buy thesis, P&L, and tax classification
- Today's market analysis (with extended thinking toggle)
- Recent closed trades
- Tax summary (ST/LT breakdown)
- Living strategy playbook
- Claude API cost tracker (with daily spend progress bar)

Auto-refreshes every 5 minutes.

---

## Useful Commands

```bash
# Logs
docker compose logs tradebot -f
docker compose logs tradebot --tail=50

# Rebuild after code changes (keeps postgres data)
docker compose up -d --build tradebot

# Shell into the app container
docker compose exec tradebot sh

# PostgreSQL shell
docker compose exec postgres psql -U scorched scorched

# Force a fresh recommendation run (bypass today's cache)
curl -s -X POST http://localhost:8000/api/v1/recommendations/generate \
  -H "Content-Type: application/json" \
  -d '{"force": true}'

# Wipe database (destructive!)
docker compose down -v
```

---

## Security

**If running on a public VM, do not expose port 8000 directly to the internet.**

The recommended setup is one of:
- **Tailscale or WireGuard VPN** — only your devices can reach the bot
- **Reverse proxy with auth** — nginx or Caddy with basic auth or client certificates
- **Cloud firewall** — restrict port 8000 to your IP only (`sudo ufw allow from YOUR_IP to any port 8000`)

MCP mutation tools (`confirm_trade`, `reject_recommendation`, `get_recommendations`) require the owner PIN when `SETTINGS_PIN` is configured. Read-only tools (`get_portfolio`, `get_market_summary`, `read_playbook`, `get_opening_prices`) do not require a PIN.

REST mutation endpoints also require the PIN via the `X-Owner-Pin` header.
