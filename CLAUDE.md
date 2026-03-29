# Scorched — Claude Code Context

## Commands

```bash
# Run locally (outside Docker, needs .env with DATABASE_URL)
uvicorn scorched.main:app --reload --port 8000

# Docker (preferred)
docker compose up -d --build
docker compose logs tradebot -f

# Rebuild after code changes (keeps postgres data)
docker compose up -d --build tradebot

# Alembic migrations
alembic upgrade head
alembic revision --autogenerate -m "description"

# Deploy to VM (from Mac — never overwrites .env on VM)
rsync -av -e "ssh -i ~/Downloads/ssh-key-2026-02-19-private.key" \
  --exclude='.venv' --exclude='.git' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='.env' \
  ~/Projects/claude/tradebot/ ubuntu@129.213.37.242:~/tradebot/

# SSH to VM
ssh -i ~/Downloads/ssh-key-2026-02-19-private.key ubuntu@129.213.37.242

# Edit crontab on VM
crontab -e
```

## Architecture

FastAPI app (`src/scorched/main.py`) with two transports:
- **MCP** at `/mcp` — Streamable HTTP, for any MCP client (currently unused in production; cron drives everything)
- **REST** at `/api/v1/` — same logic via standard HTTP; cron jobs hit these endpoints directly

The daily cycle is driven by **cron jobs on the VM** — no AI orchestrator required:
- `30 12 * * 1-5` UTC (8:30 AM ET) → Phase 1: POST `/api/v1/recommendations/generate`
- `30 13 * * 1-5` UTC (9:30 AM ET) → Phase 1.5: Circuit breaker gate (`cron/tradebot_phase1_5.py`)
- `35 13 * * 1-5` UTC (9:35 AM ET) → Phase 2: POST `/api/v1/trades/confirm` for each cleared rec
- `02 20 * * 1-5` UTC (4:02 PM ET) → Phase 3: EOD summary + playbook update

The MCP sub-app has a lifespan issue: FastAPI doesn't propagate lifespan to mounted sub-apps. `mcp.session_manager.run()` is wired manually into FastAPI's own `lifespan` context manager. Don't break this.

`streamable_http_path = "/"` must be set before calling `mcp.streamable_http_app()` — otherwise FastMCP registers its route at `/mcp` internally and the FastAPI mount at `/mcp` creates a `/mcp/mcp` double-prefix.

## Key Files

| File | Role |
|------|------|
| `src/scorched/main.py` | FastAPI app + MCP mount + lifespan wiring |
| `src/scorched/mcp_tools.py` | 7 MCP tool definitions (FastMCP) |
| `src/scorched/services/recommender.py` | Claude pipeline (4-call: analysis → decision → risk review → position mgmt), NYSE holiday check, recommendation cache |
| `src/scorched/services/research.py` | All data fetching: yfinance, FRED, Polygon, Alpha Vantage, EDGAR, Finnhub, momentum screener, options |
| `src/scorched/services/technicals.py` | MACD, Bollinger Bands, MA crossover, support/resistance, volume profile calculations |
| `src/scorched/services/finnhub_data.py` | Analyst consensus ratings and price targets from Finnhub |
| `src/scorched/services/risk_review.py` | Call 3: Adversarial risk committee review of recommendations |
| `src/scorched/services/position_mgmt.py` | Call 4: EOD position management review and stop suggestions |
| `src/scorched/services/portfolio.py` | apply_buy(), apply_sell(), get_portfolio_state() |
| `src/scorched/services/playbook.py` | Playbook read/update (living strategy doc) |
| `src/scorched/services/strategy.py` | load_strategy() — reads strategy.json (edited via dashboard) |
| `src/scorched/tax.py` | ST/LT classification based on first_purchase_date |
| `src/scorched/broker/` | BrokerAdapter ABC, PaperBroker, AlpacaBroker, `get_broker()` factory |
| `src/scorched/circuit_breaker.py` | Pre-execution gate checks (stock gap, SPY drop, VIX spike) |
| `src/scorched/cost.py` | Claude token cost calculator + record_usage() |
| `src/scorched/api_tracker.py` | API call tracking — sync recorder, health aggregation, cleanup |
| `src/scorched/api/system.py` | System health endpoints: /system/health, /system/errors, /system/trend |
| `src/scorched/models.py` | 8 SQLAlchemy ORM models (including ApiCallLog) |
| `src/scorched/schemas.py` | Pydantic request/response schemas |
| `src/scorched/config.py` | pydantic-settings Settings (env vars, tax rates, cash reserve %) |
| `strategy.md` | Human-readable strategy reference (source of truth is strategy.json via dashboard) |
| `analyst_guidance.md` | Signal interpretation tables + hard rules injected into both Claude prompts at runtime |
| `alembic/versions/` | DB migrations — always generate, never hand-edit |

## Claude Pipeline (recommender.py)

Four API calls per day, all using `claude-sonnet-4-6`:

**Call 1 — Analysis** (extended thinking, budget=16000 tokens):
- System: `ANALYSIS_SYSTEM` — analyst persona, strategy injected
- Input: market context + full research context (price data, technicals, analyst consensus, news, macro, earnings, insider activity)
- Output: `{"analysis": "...", "candidates": ["TICK1", ...]}`

**Call 2 — Decision** (standard, no extended thinking):
- System: `DECISION_SYSTEM` — trader persona, strategy + playbook injected
- Input: analysis text + options data for candidates + current portfolio
- Output: `{"research_summary": "...", "recommendations": [...]}`

**Call 3 — Risk Committee** (standard, no extended thinking):
- System: `RISK_REVIEW_SYSTEM` — skeptical risk reviewer, default-reject stance
- Input: proposed recommendations + portfolio + analysis summary + recent playbook
- Output: `{"decisions": [{"symbol": ..., "verdict": "approve"|"reject", ...}]}`
- Rejected buys are removed before saving. Sells always pass through.

**Call 4 — Position Management** (EOD, standard):
- System: `POSITION_MGMT_SYSTEM` — conservative position reviewer
- Input: all open positions with current prices + today's market summary
- Output: per-position hold/tighten/partial/exit recommendations (logged)

## Data Sources

| Source | What it provides | Key? |
|--------|-----------------|------|
| yfinance | Price history, fundamentals, news, earnings dates, options chains, insider purchases | No |
| FRED | Fed funds rate, 10Y/2Y yields, CPI, unemployment, retail sales, HY credit spread, PCE, industrial production | `FRED_API_KEY` |
| Polygon.io | News headlines + article descriptions (paid tier provides full summaries) | `POLYGON_API_KEY` |
| Alpha Vantage | RSI(14) for screener picks only (≤20 symbols, free tier = 25 calls/day) | `ALPHA_VANTAGE_API_KEY` |
| Finnhub | Analyst consensus ratings, price targets, recommendation trends | `FINNHUB_API_KEY` |
| SEC EDGAR | Form 4 insider buy/sell data (free, no key) | No |
| Momentum screener | Top 30 S&P 500 members by 5-day momentum (price > 20d MA, volume > 1M) | No |
| Technical analysis | MACD, Bollinger Bands, 50/200 MA crossover, support/resistance, volume profile (computed from yfinance history) | No |
| Options (yfinance) | Put/call ratio, ATM IV, implied 30-day move — fetched only for candidates | No |

## Settings (config.py)

| Setting | Default | Notes |
|---------|---------|-------|
| `STARTING_CAPITAL` | $100,000 | Set in .env |
| `SHORT_TERM_TAX_RATE` | 37% | Applied to gains held < 365 days |
| `LONG_TERM_TAX_RATE` | 20% | Applied to gains held ≥ 365 days |
| `min_cash_reserve_pct` | 10% | Hard floor; buys that violate this are skipped |
| `FRED_API_KEY` | "" | Empty = FRED data skipped |
| `ALPHA_VANTAGE_API_KEY` | "" | Empty = RSI data skipped |
| `POLYGON_API_KEY` | "" | Empty = Polygon news skipped |
| `FINNHUB_API_KEY` | "" | Empty = analyst consensus skipped |
| `settings_pin` | "" | If set, PUT /api/v1/strategy requires this PIN |
| `BROKER_MODE` | "paper" | "paper" = DB-only, "alpaca_paper" = Alpaca paper, "alpaca_live" = Alpaca live |
| `ALPACA_API_KEY` | "" | Required for alpaca_paper / alpaca_live modes |
| `ALPACA_SECRET_KEY` | "" | Required for alpaca_paper / alpaca_live modes |

## Broker Integration (Alpaca)

The system supports three broker modes, controlled by `BROKER_MODE` in `.env`:

| Mode | Behavior |
|------|----------|
| `paper` (default) | DB-only trades, no broker. Original behavior. |
| `alpaca_paper` | Orders go to Alpaca paper trading. Fills recorded in local DB. |
| `alpaca_live` | Orders go to Alpaca live trading. Real money. |

**Architecture:** `BrokerAdapter` ABC in `src/scorched/broker/` with `PaperBroker` and `AlpacaBroker` implementations. `get_broker(db)` factory reads `settings.broker_mode`. The trade confirmation endpoint (`POST /api/v1/trades/confirm`) routes through the broker adapter — no other endpoints change.

**AlpacaBroker flow:** Submits limit orders via `alpaca-py` SDK → polls for fill (2s interval, 60s timeout) → mirrors fill into local DB (Portfolio, Position, TradeHistory) for dashboard/tax consistency.

**Circuit Breaker (Phase 1.5):** Runs at 9:30 AM ET, between Phase 1 (recommendations) and Phase 2 (execution). Gates buy orders based on:
- Individual stock gap-down from prior close (default: >2%)
- Price drift from Claude's suggested price (default: >1.5%)
- SPY gap-down (default: >1%)
- VIX absolute level (default: >30) or overnight spike (default: >20%)

Thresholds are configurable in `strategy.json` under `circuit_breaker`. Sells always pass through.

**Position Reconciliation:** `GET /api/v1/broker/status` compares local DB positions against Alpaca holdings and flags mismatches.

**System Health:** All external API calls are tracked in `api_call_log` table. Dashboard header shows R/Y/G summary. `/system` page has full operational detail (per-API cards, error log, 7-day trend). Records auto-cleaned after 30 days.

## Gotchas

- **yfinance is sync** — all yfinance calls in `research.py` are wrapped in `asyncio.run_in_executor`. Don't call yfinance directly in async context.
- **Database seeding** happens at startup in `lifespan` — portfolio row is created if empty. Safe to run multiple times.
- **`force: true` on recommendations** — NULLs out `token_usage.session_id` (nullable FK) before deleting the session, avoiding FK violation. Don't skip this step.
- **Recommendation caching** — `get_recommendations` returns the existing session if one exists for today, unless `force=True`. This is intentional.
- **NYSE holidays** — detected in `_is_market_open()` using `pandas_market_calendars`. Returns early before any DB or Claude work.
- **VM cron times are UTC** — DST (US clocks spring forward ~March 8): ET shifts from UTC-5 → UTC-4. After DST: Phase 1 at `30 12`, Phase 1.5 at `30 13`, Phase 2 at `35 13`, Phase 3 at `02 20`. Before DST: shift each forward 1hr.
- **`.env` on VM must not be overwritten** — rsync command uses `--exclude='.env'`. The local `.env` is a placeholder; the real API key lives only on the VM.
- **Suggested price override** — after Claude outputs `suggested_price`, the code replaces it with the live price fetched from yfinance before saving to DB.
- **Wash sale detection** — buys within 30 days of a same-symbol sell get a `⚠️ WASH SALE WARNING` prepended to `key_risks`.
- **Model** is `claude-sonnet-4-6`. THINKING_BUDGET is 16000 tokens. 4 Claude calls/day: analysis, decision, risk review, position management.

## Environment

Required in `.env`:
```
ANTHROPIC_API_KEY=sk-ant-api03-...
STARTING_CAPITAL=100000
```

Optional (for Alpaca broker integration):
```
BROKER_MODE=alpaca_paper
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
```

`DATABASE_URL` is injected by Docker Compose. Only needed for local non-Docker runs:
```
DATABASE_URL=postgresql+asyncpg://scorched:scorched@localhost:5432/scorched
```
