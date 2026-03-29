# Scorched — Simulated Stock Trading Bot

A paper-trading bot that researches stocks each morning, generates buy/sell recommendations via Claude, and tracks a simulated portfolio with P&L and tax implications. Runs fully autonomously on a daily cron schedule — no manual intervention or AI orchestrator required.

**Paper trading only** — no real money, no real brokerage integration.

---

## How It Works

```
Cron (VM)
    │
    │  8:30 AM ET — POST /api/v1/recommendations/generate
    │  9:45 AM ET — POST /api/v1/trades/confirm × N
    │  Every 5 min (9:35 AM–3:55 PM ET) — POST /api/v1/intraday/evaluate
    │
    ▼
Scorched (FastAPI + PostgreSQL)
    │
    ├── Fetches market data (yfinance, FRED, Polygon, Alpha Vantage, EDGAR)
    ├── Runs momentum screener (top S&P 500 movers)
    ├── Calls Claude (claude-sonnet-4-6) — two-call pipeline
    │     Call 1: Analysis w/ extended thinking → identify candidates
    │     Call 2: Decision → 0–3 concrete trade recommendations
    ├── Tracks portfolio state in PostgreSQL
    └── Dashboard auto-refreshes at http://host:8000
```

**Daily cycle:**
1. **8:30 AM ET (pre-market)** — `generate` fetches live market data across the full research universe, builds a rich context packet, and asks Claude for up to 3 trades. Results are cached; safe to call multiple times.
2. **9:45 AM ET (post-open)** — cron script fetches actual opening prices via `get_opening_prices` and calls `confirm_trade` for each pending recommendation, filling at the real open price for accurate simulation.
3. **9:35 AM–3:55 PM ET (every 5 min)** — intraday monitor checks held positions against 5 configurable triggers (position drop from entry, drop from open, SPY drop, VIX spike, volume surge). If any trigger fires, Claude evaluates whether to exit. Zero LLM cost on quiet days.
4. **4:01 PM ET (post-close)** — EOD review, playbook update, summary notification.

**NYSE holidays** are detected automatically — if the market is closed, `generate` returns `market_closed: true` with no recommendations and no Claude calls are made.

---

## Stack

| Layer | Technology |
|-------|-----------|
| Runtime | Python 3.11, FastAPI |
| AI | Anthropic Claude (`claude-sonnet-4-6`, extended thinking on Call 1) |
| MCP | `mcp[cli]` (FastMCP, Streamable HTTP transport) |
| Database | PostgreSQL 16 via SQLAlchemy 2.0 async + asyncpg |
| Migrations | Alembic |
| Market data | yfinance (prices, fundamentals, options, earnings, news, insider) |
| Macro data | FRED API (Fed rate, CPI, yield curve, PCE, credit spreads) |
| News | Polygon.io (preferred) + yfinance fallback |
| Technicals | Alpha Vantage RSI(14) for screener picks |
| Insider filings | SEC EDGAR Form 4 (free, no key) |
| Holiday detection | `pandas-market-calendars` (NYSE calendar) |
| Automation | cron on the VM |
| Deployment | Docker Compose |

---

## MCP Tools

The bot exposes 7 tools over MCP at `http://host:8000/mcp`. These mirror the REST endpoints and are available for any MCP client:

| Tool | When | Description |
|------|------|-------------|
| `get_recommendations` | 8:30 AM ET | Research stocks, ask Claude, return ≤3 picks. Cached per day. |
| `get_opening_prices` | 9:45 AM ET | Fetch actual opening auction prices for a list of symbols. |
| `confirm_trade` | 9:45 AM ET | Record a trade execution; updates portfolio state. |
| `reject_recommendation` | 9:45 AM ET | Mark a recommendation as skipped (keeps audit trail clean). |
| `get_portfolio` | Anytime | Live portfolio snapshot with unrealized P&L and tax estimates. |
| `get_market_summary` | After 4 PM ET | EOD index + all S&P sector ETF performance. |
| `read_playbook` | Optional | Read the bot's living strategy document. |

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

7 tables in PostgreSQL:

| Table | Purpose |
|-------|---------|
| `portfolio` | Single-row: cash balance and starting capital |
| `positions` | One row per held ticker; avg cost basis updated on each buy |
| `recommendation_sessions` | One row per trading day; caches raw research + Claude response + analysis thinking |
| `trade_recommendations` | Up to 3 rows per session; status: `pending` → `confirmed`/`rejected` |
| `trade_history` | Append-only audit log of executed trades |
| `playbook` | Single-row living strategy document (updated by Claude before each session) |
| `token_usage` | Per-call Claude API token tracking (input, output, thinking tokens) |

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
├── DEPLOY.md               # Full deployment + cron guide
├── alembic/
│   ├── env.py
│   └── versions/           # Migration files
└── src/
    └── scorched/
        ├── main.py         # FastAPI app; mounts MCP at /mcp
        ├── config.py       # pydantic-settings Settings
        ├── database.py     # Async SQLAlchemy engine + session
        ├── models.py       # 7 ORM models
        ├── schemas.py      # Pydantic request/response schemas
        ├── mcp_tools.py    # 7 MCP tool definitions (FastMCP)
        ├── tax.py          # classify_gain(), estimate_tax()
        ├── cost.py         # Claude token cost calculator
        ├── static/
        │   └── dashboard.html
        ├── api/            # FastAPI routers
        │   ├── costs.py
        │   ├── market.py
        │   ├── playbook.py
        │   ├── portfolio.py
        │   ├── recommendations.py
        │   ├── strategy.py
        │   └── trades.py
        └── services/
            ├── portfolio.py    # apply_buy(), apply_sell(), get_portfolio_state()
            ├── recommender.py  # Claude two-call pipeline + NYSE holiday check
            ├── research.py     # All data fetching (yfinance, FRED, Polygon, AV, EDGAR)
            ├── playbook.py     # Playbook read/update
            └── strategy.py     # load_strategy() from strategy.json
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

# Optional: require a PIN to update strategy via dashboard
SETTINGS_PIN=
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
