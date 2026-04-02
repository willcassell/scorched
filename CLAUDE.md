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

# Deploy to VM (never overwrites .env on VM)
rsync -av -e "ssh -i /path/to/ssh-key.key" \
  --exclude='.venv' --exclude='.git' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='.env' \
  /path/to/tradebot/ ubuntu@YOUR_VM_IP:~/tradebot/

# SSH to VM
ssh -i /path/to/ssh-key.key ubuntu@YOUR_VM_IP

# Edit crontab on VM
crontab -e
```

## Architecture

FastAPI app (`src/scorched/main.py`) with two transports:
- **MCP** at `/mcp` — Streamable HTTP, for any MCP client (currently unused in production; cron drives everything)
- **REST** at `/api/v1/` — same logic via standard HTTP; cron jobs hit these endpoints directly

The daily cycle is driven by **cron jobs on the VM** — no AI orchestrator required:
- `30 7 * * 1-5` ET (7:30 AM ET) → Phase 0: POST `/api/v1/research/prefetch` (data fetch, zero LLM cost)
- `30 8 * * 1-5` ET (8:30 AM ET) → Phase 1: POST `/api/v1/recommendations/generate` (loads Phase 0 cache, Claude calls only)
- `30 9 * * 1-5` ET (9:30 AM ET) → Phase 1.5: Circuit breaker gate (`cron/tradebot_phase1_5.py`)
- `35 9 * * 1-5` ET (9:35 AM ET) → Phase 2: POST `/api/v1/trades/confirm` for each cleared rec
- `*/5 9-15 * * 1-5` ET → Intraday: Position monitoring with trigger-based Claude exit evaluation (9:35 AM–3:55 PM ET, self-gates)
- `01 16 * * 1-5` ET (4:01 PM ET) → Phase 3: EOD summary + playbook update

The MCP sub-app has a lifespan issue: FastAPI doesn't propagate lifespan to mounted sub-apps. `mcp.session_manager.run()` is wired manually into FastAPI's own `lifespan` context manager. Don't break this.

`streamable_http_path = "/"` must be set before calling `mcp.streamable_http_app()` — otherwise FastMCP registers its route at `/mcp` internally and the FastAPI mount at `/mcp` creates a `/mcp/mcp` double-prefix.

## Key Files

| File | Role |
|------|------|
| `src/scorched/main.py` | FastAPI app + MCP mount + lifespan wiring |
| `src/scorched/mcp_tools.py` | 7 MCP tool definitions (FastMCP) |
| `src/scorched/api/prefetch.py` | Phase 0: POST /api/v1/research/prefetch — fetches all external data, caches for Phase 1 |
| `src/scorched/services/recommender.py` | Claude pipeline (4-call: analysis → decision → risk review → position mgmt), loads Phase 0 cache or falls back to inline fetch |
| `src/scorched/services/research.py` | All data fetching: yfinance, FRED, Polygon, Alpha Vantage, EDGAR, Finnhub, momentum screener, options |
| `src/scorched/services/technicals.py` | MACD, Bollinger Bands, MA crossover, support/resistance, volume profile, ATR calculations |
| `src/scorched/services/finnhub_data.py` | Analyst consensus ratings and price targets from Finnhub |
| `src/scorched/services/risk_review.py` | Call 3: Adversarial risk committee review of recommendations |
| `src/scorched/services/position_mgmt.py` | Call 4: EOD position management review and stop suggestions |
| `src/scorched/services/portfolio.py` | apply_buy(), apply_sell(), get_portfolio_state() |
| `src/scorched/services/playbook.py` | Playbook read/update (living strategy doc) |
| `src/scorched/services/strategy.py` | load_strategy() — reads strategy.json (edited via dashboard) |
| `src/scorched/tax.py` | ST/LT classification based on first_purchase_date |
| `src/scorched/broker/` | BrokerAdapter ABC, PaperBroker, AlpacaBroker, `get_broker()` factory |
| `src/scorched/drawdown_gate.py` | Portfolio drawdown enforcement — blocks buys when down >8% from peak |
| `src/scorched/correlation.py` | 20-day return correlation between candidates and held positions |
| `src/scorched/http_retry.py` | Retry wrapper for external HTTP APIs (3 attempts, 1s/3s/5s backoff) |
| `src/scorched/circuit_breaker.py` | Pre-execution gate checks (stock gap, SPY drop, VIX spike) |
| `src/scorched/broker/pending_fills.py` | Crash recovery: JSON-based pending fill records for Alpaca trades |
| `src/scorched/cost.py` | Claude token cost calculator + record_usage() |
| `src/scorched/api_tracker.py` | API call tracking — sync recorder, health aggregation, cleanup |
| `src/scorched/intraday.py` | Pure intraday trigger check functions |
| `src/scorched/api/intraday.py` | POST /api/v1/intraday/evaluate — Claude exit evaluation + auto-sell |
| `cron/tradebot_phase0.py` | Phase 0 cron: calls /api/v1/research/prefetch, sends timing via Telegram |
| `cron/intraday_monitor.py` | Every 5 min position check during market hours |
| `src/scorched/api/system.py` | System health endpoints: /system/health, /system/errors, /system/trend |
| `src/scorched/models.py` | 8 SQLAlchemy ORM models (including ApiCallLog) |
| `src/scorched/schemas.py` | Pydantic request/response schemas |
| `src/scorched/config.py` | pydantic-settings Settings (env vars, tax rates, cash reserve %) |
| `strategy.md` | Human-readable strategy reference (source of truth is strategy.json via dashboard) |
| `analyst_guidance.md` | Signal interpretation tables + hard rules injected into both Claude prompts at runtime |
| `alembic/versions/` | DB migrations — always generate, never hand-edit |

## Claude Pipeline (recommender.py)

Four API calls per day. Calls 1-3 use `claude-sonnet-4-6`; EOD review and intraday exit use `claude-haiku-4-5-20251001`; playbook update uses `claude-sonnet-4-6` (was opus, switched for cost savings):

**Call 1 — Analysis** (extended thinking, budget=16000 tokens):
- System: `ANALYSIS_SYSTEM` — analyst persona, strategy injected, 6-step structured framework (macro → sector → screening → ranking → position review → output)
- Input: market context + pre-filtered research context (top 25 symbols + held positions, with relative strength and ATR)
- Output: `{"analysis": "...", "candidates": ["TICK1", ...]}` — validated via Pydantic `AnalysisOutput`

**Call 2 — Decision** (standard, no extended thinking):
- System: `DECISION_SYSTEM` — trader persona, strategy + playbook injected, few-shot examples
- Input: analysis text + options data for candidates + current portfolio
- Output: `{"research_summary": "...", "recommendations": [...]}` — validated via Pydantic `DecisionOutput`

**Call 3 — Risk Committee** (standard, no extended thinking):
- System: `RISK_REVIEW_SYSTEM` — skeptical risk reviewer, default-reject stance
- Input: proposed recommendations + portfolio + full analysis (3000 chars) + playbook (1500 chars) + correlation warnings
- Output: `{"decisions": [{"symbol": ..., "verdict": "approve"|"reject", ...}]}` — validated via Pydantic `RiskReviewOutput`
- Rejected buys are removed before saving. Sells always pass through.

**Call 4 — Position Management** (EOD, standard):
- System: `POSITION_MGMT_SYSTEM` — conservative position reviewer
- Input: all open positions with current prices + today's market summary
- Output: per-position hold/tighten/partial/exit recommendations (logged)

**Call 5+ — Intraday Exit** (conditional, during market hours):
- Triggered only when intraday monitor detects a position hitting one of 5 configurable triggers
- Prompt: `src/scorched/prompts/intraday_exit.md`
- Input: triggered position data + trigger details + current market conditions
- Output: exit/hold decision with reasoning; auto-executes sells
- Cost: ~$0.01 per triggered position; zero LLM cost on quiet days
- Triggers (configurable in `strategy.json` under `intraday_monitor`): position drop from entry (5%), drop from today's open (3%), SPY intraday drop (2%), VIX above threshold (30), volume surge (3x average)

## Data Sources

| Source | What it provides | Key? |
|--------|-----------------|------|
| yfinance | Price history, fundamentals, news, earnings dates, options chains, insider purchases | No |
| FRED | Fed funds rate, 10Y/2Y yields, CPI, unemployment, retail sales, HY credit spread, PCE, industrial production | `FRED_API_KEY` |
| Polygon.io | News headlines + article descriptions (paid tier provides full summaries) | `POLYGON_API_KEY` |
| Alpha Vantage | RSI(14) for screener picks only (≤20 symbols, free tier = 25 calls/day) | `ALPHA_VANTAGE_API_KEY` |
| Finnhub | Analyst consensus ratings, price targets, recommendation trends | `FINNHUB_API_KEY` |
| SEC EDGAR | Form 4 insider filing counts (free, no key; type unknown from API) | No |
| Momentum screener | Top 20 S&P 500 by 5-day momentum (batch `yf.download()`, price > 20d MA, vol > 1M) | No |
| Sector ETFs | 5-day returns for 11 sector ETFs (XLK, XLF, etc.) for relative strength calc | No |
| Technical analysis | MACD, Bollinger Bands, 50/200 MA crossover, support/resistance, volume profile, ATR (computed from yfinance history) | No |
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
- **Phase 0 cache** — `generate_recommendations()` checks for `/tmp/tradebot_research_cache_{date}.json` written by Phase 0. If found, skips all data fetches and uses cached data. If missing, falls back to inline fetch.
- **VM cron times are ET** — DST (US clocks spring forward ~March 8): no crontab change needed since cron uses ET directly. Each cron script has a `check_expected_hour()` call that sends a Telegram warning if it runs at the wrong ET hour.
- **`.env` on VM must not be overwritten** — rsync command uses `--exclude='.env'`. The local `.env` is a placeholder; the real API key lives only on the VM.
- **Suggested price override** — after Claude outputs `suggested_price`, the code replaces it with the live price fetched from yfinance before saving to DB.
- **Wash sale detection** — buys within 30 days of a same-symbol sell get a `⚠️ WASH SALE WARNING` prepended to `key_risks`.
- **Models**: Calls 1-3 + playbook use `claude-sonnet-4-6`. EOD review + intraday exit use `claude-haiku-4-5-20251001`. THINKING_BUDGET is 16000 tokens. Up to 7 Claude calls/day: analysis, decision, risk review, position mgmt, EOD review, playbook update, + intraday exit (only when triggered, typically 0).
- **Context pre-filter** — `build_research_context()` scores all symbols and only sends top 25 (plus held positions) to Claude, reducing context by ~50%. Scoring: momentum + news catalyst + technical alignment + volume + relative strength + insider activity.
- **Relative strength** — each symbol's 5-day return is compared to its sector ETF return. Displayed as "vs sector: +X.X% (outperforming/underperforming)" in the research context.
- **ATR** — 14-day Average True Range computed in technicals.py. Displayed as ATR: $X.XX (Y.Y%) for volatility-adjusted stop guidance.
- **Endpoint auth** — all POST/PUT mutation endpoints require `X-Owner-Pin` header when `SETTINGS_PIN` is set. Cron scripts pass it automatically via `common.py:http_post()`.
- **Benchmarks** — portfolio return compared against SPY, QQQ, RSP (equal weight), MTUM (momentum factor), SPMO (S&P momentum). DJI was removed. Trade performance metrics (win rate, profit factor, expectancy, max drawdown, avg holding period) computed from TradeHistory.
- **Drawdown gate** — blocks all BUY recommendations when portfolio drops >8% from peak equity (configurable in `strategy.json` under `drawdown_gate`). Sells always pass. Peak is tracked in `Portfolio.peak_portfolio_value` column.
- **Correlation check** — before accepting a BUY, computes 20-day return correlation with all held positions. r > 0.8 triggers a warning prepended to `key_risks` and a CORRELATION WARNINGS section in the risk review prompt.
- **Crash recovery** — Alpaca fills write a pending-fill JSON record before DB recording. On startup, `main.py` reconciles any unrecorded fills. Records live at `/app/logs/pending_fills.json` (Docker volume).
- **AlpacaBroker delegates to apply_buy/apply_sell** — no longer has duplicate portfolio logic. Same pattern as PaperBroker.
- **Pydantic validation** — all Claude JSON outputs validated via `AnalysisOutput`, `DecisionOutput`, `RiskReviewOutput` models. Graceful fallback on validation failure.
- **HTTP retry** — external data APIs (FRED, Polygon, Alpha Vantage, EDGAR, Finnhub) wrapped with `retry_get`/`retry_call` (3 attempts, 1s/3s/5s backoff on transient errors only).
- **Timezone-aware datetimes** — all `datetime.utcnow()` replaced with `datetime.now(timezone.utc)`. For DB queries against `TIMESTAMP WITHOUT TIME ZONE` columns, use `.replace(tzinfo=None)` to avoid asyncpg comparison errors.

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
