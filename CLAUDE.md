# Scorched — Claude Code Context

> ## ⛔ RETIRED — SHUT DOWN 2026-08-31
>
> **This system is not running.** All 9 tradebot cron jobs are disabled, the Docker stack is
> down, and the dashboard is offline. Experiment C was retired ~8 weeks early by owner decision
> after `evaluate_experiment.py` returned RETIRE on both kill criteria.
>
> **Final result:** +1.95% (Alpaca, broker of record) over six months, against SPY +10.75% and
> SPMO +22.16%. 50.8% win rate, profit factor 0.887. Trade-attributable P&L was ~flat (+$5.68);
> the headline +2% is mostly a 2026-04-18 reconciliation correction, not trading skill.
>
> **Two positions remain open and UNMONITORED** in the Alpaca paper account: MA (24) and
> PFE (500). Paper money, no financial risk, but their trailing stops will not fire.
>
> - Full record: [`SHUTDOWN.md`](SHUTDOWN.md) (also in `.handovers/2026-08-31-shutdown.md`, local-only)
> - Data archive: `/home/ubuntu/tradebot-archive/` (pg_dump + CSVs + logs, outside git)
> - Crontab backup: `/home/ubuntu/crontab.backup.2026-08-31-shutdown`
>
> Everything below describes the system **as it was when running**, and is retained as
> reference. Restart instructions are in the handover — re-run `/broker/sync` and reset the
> `experiment` block in `strategy.json` before trusting anything.

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
  /path/to/scorched/ ubuntu@YOUR_VM_IP:~/scorched/

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
- `35 9 * * 1-5` ET (9:35 AM ET) → Phase 0: POST `/api/v1/research/prefetch` (post-open data fetch with live gaps/volume, zero LLM cost)
- `45 9 * * 1-5` ET (9:45 AM ET) → Phase 1: POST `/api/v1/recommendations/generate` (Claude sees real opening data, not pre-market guesses)
- `55 9 * * 1-5` ET (9:55 AM ET) → Phase 1.5: Circuit breaker gate with 25 min of live data (`cron/tradebot_phase1_5.py`)
- `15 10 * * 1-5` ET (10:15 AM ET) → Phase 2: POST `/api/v1/trades/confirm` for each cleared rec (fire-and-forget for Alpaca, post opening range)
- `45 10 * * 1-5` ET (10:45 AM ET) → Phase 2.5: POST `/api/v1/trades/reconcile` + POST `/api/v1/broker/sync` — reconciles pending fills, then syncs positions against Alpaca
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
| `src/scorched/services/alpaca_data.py` | Alpaca Data API: snapshots, bars (IEX), news, screener — replaces yfinance for prices and Polygon for news |
| `src/scorched/services/research.py` | Data orchestration: Alpaca (prices/news), yfinance (fundamentals/options), FRED, Alpha Vantage, EDGAR, Finnhub |
| `src/scorched/services/technicals.py` | MACD, Bollinger Bands, MA crossover, support/resistance, volume profile, ATR, GARCH(1,1) forward-vol forecast |
| `src/scorched/services/risk.py` | Portfolio-level historical-simulation VaR & CVaR (1-day, configurable confidence and lookback) |
| `src/scorched/services/backtest.py` | `replay_with_alternate_exits()` (re-exit actual TradeHistory buys with new rules) + `simulate_breakout_strategy()` (parameterized rule replay over Alpaca bars) + shared `compute_metrics()` |
| `scripts/backtest.py` | CLI: `python scripts/backtest.py replay --stop-pct 0.06` or `sim --symbols AAPL,MSFT --vol-mult 1.0`. Validates entry/exit edits before shipping |
| `src/scorched/services/economic_calendar.py` | FRED-based upcoming economic release tracking (CPI, Jobs, FOMC, GDP, etc.) |
| `src/scorched/services/finnhub_data.py` | Analyst consensus ratings and recommendation trends from Finnhub |
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
| `src/scorched/trailing_stops.py` | ATR-based trailing stop logic — pure functions; HWM ratchet + `check_trailing_stop_breach()` called by intraday monitor |
| `src/scorched/services/reflection.py` | Weekly trade reflection: reviews outcomes, extracts learnings |
| `src/scorched/broker/pending_fills.py` | Pending fill tracking: submitted orders awaiting Alpaca fill + crash recovery |
| `cron/tradebot_reconcile.py` | Phase 2.5 cron: reconciles pending Alpaca orders, then syncs positions against Alpaca |
| `src/scorched/services/reconciliation.py` | Position check + sync: compares local DB vs Alpaca, auto-corrects mismatches |
| `src/scorched/cost.py` | Claude token cost calculator + record_usage() |
| `src/scorched/api_tracker.py` | API call tracking — sync recorder, health aggregation, cleanup |
| `src/scorched/tz.py` | Timezone utility: `market_today()`, `market_now()`, `MARKET_TZ` — all trading-day logic uses this |
| `src/scorched/intraday.py` | Pure intraday trigger check functions |
| `src/scorched/api/intraday.py` | POST /api/v1/intraday/evaluate — Claude exit evaluation + auto-sell |
| `cron/tradebot_phase0.py` | Phase 0 cron: calls /api/v1/research/prefetch, sends timing via Telegram |
| `cron/intraday_monitor.py` | Every 5 min position check during market hours |
| `scripts/setup_cron.py` | Automated cron setup — auto-detects DST, installs/removes/checks cron jobs |
| `src/scorched/api/system.py` | System health endpoints: /system/health, /system/errors, /system/trend, /system/market-date |
| `src/scorched/models.py` | 9 SQLAlchemy ORM models (including ApiCallLog, EquityHistory) |
| `src/scorched/services/equity_history.py` | Daily equity snapshots — `record_snapshot()` (idempotent per NYSE day, called from Phase 3 EOD) + `get_history()` |
| `src/scorched/schemas.py` | Pydantic request/response schemas |
| `src/scorched/config.py` | pydantic-settings Settings (env vars, tax rates, cash reserve %) |
| `strategy.md` | Human-readable strategy reference (source of truth is strategy.json via dashboard) |
| `analyst_guidance.md` | Signal interpretation tables + hard rules injected into both Claude prompts at runtime |
| `alembic/versions/` | DB migrations — always generate, never hand-edit |

## Claude Pipeline (recommender.py)

Four API calls per day, up to 7 total. All calls use `claude-opus-5` (migrated 2026-07-31 from the sonnet/haiku split) with adaptive thinking always on; depth is controlled per call via an `effort` map in `services/claude_client.py` (`EFFORT = {"analysis": "xhigh", "decision": "high", "risk_review": "high", "position_mgmt": "medium", "intraday_exit": "high", "playbook": "medium", "reflection": "medium", "quick": "low"}`). `THINKING_BUDGET` (16000) is now `max_tokens` headroom for Call 1, not an API thinking-token-limit param — Opus 5 rejects fixed-ceiling thinking config.

**Call 1 — Analysis** (`claude-opus-5`, adaptive thinking, `display: "summarized"`, `effort: "xhigh"`, `max_tokens = THINKING_BUDGET + 4096`):
- System: `ANALYSIS_SYSTEM` — analyst persona, strategy injected, 6-step structured framework (macro → sector → screening → ranking → position review → output)
- Input: market context + pre-filtered research context (top 25 symbols + held positions, with relative strength, ATR, and economic calendar)
- Output: `{"analysis": "...", "candidates": ["TICK1", ...]}` — validated via Pydantic `AnalysisOutput`

**Call 2 — Decision** (`claude-opus-5`, adaptive thinking, `effort: "high"`):
- System: `DECISION_SYSTEM` — trader persona, strategy + playbook injected, few-shot examples
- Input: analysis text + options data for candidates + current portfolio
- Output: `{"research_summary": "...", "recommendations": [...]}` — validated via Pydantic `DecisionOutput`

**Call 3 — Risk Committee** (`claude-opus-5`, adaptive thinking, `effort: "high"`):
- System: `RISK_REVIEW_SYSTEM` — skeptical risk reviewer, default-reject stance
- Input: proposed recommendations + portfolio + full analysis (3000 chars) + playbook (1500 chars) + correlation warnings
- Output: `{"decisions": [{"symbol": ..., "verdict": "approve"|"reject", ...}]}` — validated via Pydantic `RiskReviewOutput`
- Rejected buys are removed before saving. Sells always pass through.

**Call 4 — Position Management** (EOD, `claude-opus-5`, adaptive thinking, `effort: "medium"`):
- System: `POSITION_MGMT_SYSTEM` — conservative position reviewer
- Input: all open positions with current prices + today's market summary
- Output: per-position hold/tighten/partial/exit recommendations (logged)

**Call 5+ — Intraday Exit** (conditional, during market hours; `claude-opus-5`, adaptive thinking, `effort: "high"`):
- Triggered only when intraday monitor detects a position hitting one of 6 configurable triggers
- Prompt: `src/scorched/prompts/intraday_exit.md`
- Input: triggered position data + trigger details + current market conditions
- Output: exit/hold decision with reasoning; auto-executes sells
- Cost: a few thousand tokens per call (e.g. a small quick-path call ran $0.00385 on 645in/25out at opus-5 rates — a full intraday-exit call with more context runs higher); zero LLM cost on quiet days
- Triggers (configurable in `strategy.json` under `intraday_monitor`): position drop from entry (5%), drop from today's open (3%), SPY intraday drop (2%), VIX above threshold (30), volume surge (3x average), trailing stop breached (ATR-based — 6th trigger, wired 2026-04-18)

JSON fix-up retries (Calls 1 and 2, on parse failure) drop to `thinking: {"type": "disabled"}` + `effort: "low"` regardless of the original call's effort — cheap by design, no extended thinking needed just to reformat. EOD review, playbook update, and weekly reflection also run on `claude-opus-5` (effort `"quick"`/`"low"`, `"medium"`, `"medium"` respectively — see `EFFORT` map in `services/claude_client.py`).

## Data Sources

| Source | What it provides | Key? |
|--------|-----------------|------|
| Alpaca Data API | Price bars (IEX, 1y daily), snapshots (current/prev close/OHLV), news (headlines+summaries), screener (most active/movers) | `ALPACA_API_KEY` (shared with broker) |
| yfinance | Fundamentals (PE, market cap, short ratio), earnings dates, options chains, insider purchases, index prices (^GSPC etc.) | No |
| FRED | Fed funds rate, 10Y/2Y yields, CPI, unemployment, retail sales, HY credit spread, PCE, industrial production | `FRED_API_KEY` |
| Polygon.io | (REMOVED — replaced by Alpaca news, fetch removed from Phase 0) | `POLYGON_API_KEY` (unused) |
| Alpha Vantage | RSI(14) for screener picks only (≤20 symbols, free tier = 25 calls/day) | `ALPHA_VANTAGE_API_KEY` |
| Twelvedata | RSI(14) for full watchlist (800 calls/day free tier) | `TWELVEDATA_API_KEY` |
| Finnhub | Analyst consensus ratings, price targets, recommendation trends | `FINNHUB_API_KEY` |
| FRED (economic calendar) | Upcoming major releases: CPI, Jobs, FOMC, GDP, PPI, PCE | `FRED_API_KEY` (same key) |
| SEC EDGAR | Form 4 insider filing counts (free, no key; type unknown from API) | No |
| Momentum screener | Top 20 S&P 500 by 5-day momentum (Alpaca bars primary, yfinance fallback; price > 20d MA, vol > 1M) | No |
| Sector ETFs | 5-day returns for 11 sector ETFs via Alpaca bars (XLK, XLF, etc.) for relative strength calc | No |
| Technical analysis | MACD, Bollinger Bands, 50/200 MA crossover, support/resistance, volume profile, ATR (computed from Alpaca bar history) | No |
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
| `POLYGON_API_KEY` | (removed) | Field dropped from config (2026-04-18); Alpaca news replaced Polygon entirely |
| `FINNHUB_API_KEY` | "" | Empty = analyst consensus skipped |
| `TWELVEDATA_API_KEY` | "" | Empty = Twelvedata RSI skipped (falls back to Alpha Vantage) |
| `settings_pin` | "" | If set, PUT /api/v1/strategy requires this PIN |
| `BROKER_MODE` | "paper" | "paper" = DB-only, "alpaca_paper" = Alpaca paper, "alpaca_live" = Alpaca live |
| `ALPACA_API_KEY` | "" | Required for broker + data API (prices, news, screener) |
| `ALPACA_SECRET_KEY` | "" | Required for broker + data API |
| `MARKET_TIMEZONE` | "America/New_York" | IANA timezone for trading day boundaries. Always NYSE time — do NOT change for user display preferences |

## Broker Integration (Alpaca)

The system supports three broker modes, controlled by `BROKER_MODE` in `.env`:

| Mode | Behavior |
|------|----------|
| `paper` (default) | DB-only trades, no broker. Original behavior. |
| `alpaca_paper` | Orders go to Alpaca paper trading. Fills recorded in local DB. |
| `alpaca_live` | Orders go to Alpaca live trading. Real money. |

**Architecture:** `BrokerAdapter` ABC in `src/scorched/broker/` with `PaperBroker` and `AlpacaBroker` implementations. `get_broker(db)` factory reads `settings.broker_mode`. The trade confirmation endpoint (`POST /api/v1/trades/confirm`) routes through the broker adapter — no other endpoints change.

**AlpacaBroker flow (fire-and-forget):** Submits limit orders via `alpaca-py` SDK → records order as pending fill → returns immediately. Phase 2.5 reconciliation cron (10:45 AM ET, 30 min later) checks all pending orders on Alpaca → records fills into local DB (Portfolio, Position, TradeHistory) → sends Telegram summary. This avoids blocking Phase 2 with polling timeouts that caused ghost positions (orders filling on Alpaca without local DB recording).

**Circuit Breaker (Phase 1.5):** Runs at 9:55 AM ET, between Phase 1 (recommendations) and Phase 2 (execution), with 25 min of live market data. Gates buy orders based on:
- Individual stock gap-down from prior close (default: >2%)
- Price drift from Claude's suggested price (default: >1.5%)
- SPY gap-down (default: >1%)
- VIX absolute level (default: >30) or overnight spike (default: >20%)

Thresholds are configurable in `strategy.json` under `circuit_breaker`. Sells always pass through.

**Position + Cash Reconciliation:** `GET /api/v1/broker/status` compares local DB against Alpaca for BOTH positions AND cash. Returns `reconciliation.cash` with `{local, broker, diff, diff_pct, status}` — `status` is `OK` / `DRIFT` / `ERROR`. Any drift ≥ $1.00 flips `has_mismatches` to true, turning the dashboard's recon indicator red. `POST /api/v1/broker/sync` reconciles positions (add/remove/adjust qty) and then replaces `Portfolio.cash_balance` with Alpaca's actual `account.cash`. Cash is NO LONGER adjusted inside the position loop using cost-basis math — that caused cumulative drift. Alpaca is source of truth for both.

**Paper-fallback sell alerts:** In `alpaca_paper` mode, `AlpacaBroker.submit_sell()` falls back to `PaperBroker` when the symbol isn't held on Alpaca (legacy positions). This silently drifts cash unless corrected. The fallback now sends a Telegram alert so the operator sees it immediately. In `alpaca_live` mode, the code refuses and raises.

**System Health:** All external API calls are tracked in `api_call_log` table. Dashboard header shows R/Y/G summary. `/system` page has full operational detail (per-API cards, error log, 7-day trend). Records auto-cleaned after 30 days.

## Gotchas

- **yfinance is sync** — all yfinance calls in `research.py` are wrapped in `asyncio.run_in_executor`. Don't call yfinance directly in async context.
- **Database seeding** happens at startup in `lifespan` — portfolio row is created if empty. Safe to run multiple times.
- **`force: true` on recommendations** — NULLs out `token_usage.session_id` (nullable FK) before deleting the session, avoiding FK violation. Don't skip this step.
- **Recommendation caching** — `get_recommendations` returns the existing session if one exists for today, unless `force=True`. This is intentional.
- **NYSE holidays** — detected in `_is_market_open()` using `pandas_market_calendars`. Returns early before any DB or Claude work.
- **Phase 0 cache** — `generate_recommendations()` checks for `/app/logs/tradebot_research_cache_{date}.json` written by Phase 0. If found, skips all data fetches. If missing, it polls for up to 120s (Phase 0 may still be running) before falling back to inline fetch. Date in filename uses `market_today()` (NYSE time).
- **VM cron times are ET** — DST (US clocks spring forward ~March 8): no crontab change needed since cron uses ET directly. Each cron script has a `check_expected_hour()` call that sends a Telegram warning if it runs at the wrong ET hour.
- **`.env` on VM must not be overwritten** — rsync command uses `--exclude='.env'`. The local `.env` is a placeholder; the real API key lives only on the VM.
- **Suggested price override** — after Claude outputs `suggested_price`, the code replaces it with the live price fetched from yfinance before saving to DB.
- **Wash sale detection** — buys within 30 days of a same-symbol sell get a `⚠️ WASH SALE WARNING` prepended to `key_risks`.
- **Models**: All calls use `claude-opus-5` (2026-07-31 migration — previously sonnet-4-6 for calls 1-3+playbook, haiku-4-5 for EOD+intraday). Depth is set per call via the `EFFORT` map, not per-model choice; `cost.py._PRICING` has the opus-5 rate ($5/$25/$25 per Mtok in/out/thinking — **thinking tokens are output tokens and must be priced at the output rate**; the third element was wrongly set to the input rate until 2026-08-05, under-reporting analysis-call cost by ~44%. Historical `token_usage` rows before that date are under-stated and were not backfilled). THINKING_BUDGET is 16000 tokens (max_tokens headroom, not a thinking-limit param). Up to 7 Claude calls/day: analysis, decision, risk review, position mgmt, EOD review, playbook update, + intraday exit (only when triggered, typically 0).
- **Context pre-filter** — `build_research_context()` scores all symbols and only sends top 25 (plus held positions) to Claude, reducing context by ~50%. Scoring: momentum + news catalyst + technical alignment + volume + relative strength + insider activity.
- **Relative strength** — each symbol's 5-day return is compared to its sector ETF return. Displayed as "vs sector: +X.X% (outperforming/underperforming)" in the research context.
- **ATR** — 14-day Average True Range computed in technicals.py. Displayed as ATR: $X.XX (Y.Y%) for volatility-adjusted stop guidance.
- **Endpoint auth** — all POST/PUT mutation endpoints require `X-Owner-Pin` header when `SETTINGS_PIN` is set. Cron scripts pass it automatically via `common.py:http_post()`.
- **Benchmarks** — portfolio return compared against SPY, QQQ, RSP (equal weight), MTUM (momentum factor), SPMO (S&P momentum). DJI was removed. Trade performance metrics (win rate, profit factor, expectancy, max drawdown, avg holding period) computed from TradeHistory.
- **Drawdown gate** — blocks all BUY recommendations when portfolio drops >8% from peak equity (configurable in `strategy.json` under `drawdown_gate`). Sells always pass. Peak is tracked in `Portfolio.peak_portfolio_value` column.
- **Correlation check** — before accepting a BUY, computes 20-day return correlation with all held positions. r > 0.8 triggers a warning prepended to `key_risks` and a CORRELATION WARNINGS section in the risk review prompt.
- **Pending fills** — `pending_fills.json` tracks submitted Alpaca orders awaiting fill confirmation. Phase 2.5 reconciliation cron checks these and records fills. Also serves as crash recovery — on startup, `main.py` reconciles any unrecorded fills. Records live at `/app/logs/pending_fills.json` (Docker volume).
- **AlpacaBroker is fire-and-forget** — `submit_buy/sell` submit the order and write a pending fill record, then return `status: "submitted"` immediately. No polling. `reconcile_pending_orders()` (called by Phase 2.5 cron) checks Alpaca for fills and calls `apply_buy/apply_sell` to record them in the local DB. PaperBroker still fills synchronously.
- **Limit order slippage buffer** — Phase 2 adds a configurable buffer to limit prices: buys at price + 0.3%, sells at price - 0.3% (configurable in `strategy.json` under `execution.buy_limit_buffer_pct` / `sell_limit_buffer_pct`). Tightened from 0.5% to 0.3% when execution moved to 10:15 AM (narrower post-opening-range spreads).
- **Alpaca Data API** — `alpaca_data.py` provides snapshots, bars (IEX feed), news, and screener via `StockHistoricalDataClient`, `NewsClient`, and `ScreenerClient`. All use IEX feed (free tier). Clients are lazy-initialized (created on first use). SIP feed requires $9/mo subscription. yfinance still used for fundamentals, options, earnings, insider data, and index symbols (^GSPC, ^VIX etc. — Alpaca only covers equities/ETFs).
- **Phase 2 at 10:15 AM** — execution at 10:15 AM avoids opening range volatility (first 30 min sees 2-3x normal volatility). Phase 2.5 reconciliation at 10:45 AM (30 min buffer for slow fills).
- **Post-open pipeline** — Phase 0 runs at 9:35 AM (5 min after open) so Claude sees real opening data (gaps, volume, sentiment) instead of stale pre-market prices. Entire pipeline (Phase 0→1→1.5) completes in ~15 min before 10:15 execution. Polygon news fetch removed (was 1010s bottleneck, redundant with yfinance headlines); Phase 0 now takes ~5 min.
- **Alpaca BarSet** — `get_stock_bars()` returns a `BarSet` Pydantic model, not a dict. Use `bars.data` to access the underlying `{symbol: [Bar, ...]}` dict — `.get()` doesn't work on `BarSet` directly.
- **Robust JSON parsing** — `parse_json_response()` in `claude_client.py` uses a 5-strategy cascade: direct `json.loads`, `raw_decode` (handles trailing text), markdown code fence extraction, first-brace scan, and brace-depth matching (handles trailing prose with unbalanced quotes). Prevents Phase 1 crashes when Claude returns JSON followed by commentary.
- **JSON fix-up retry** — If `parse_json_response` fails for Call 1 (analysis) or Call 2 (decision), the call is retried once with a multi-turn prompt: Claude's bad response as assistant turn + "respond with only the JSON" user turn. The retry runs `thinking: {"type": "disabled"}` + effort `"low"`, but resends the full call context at opus-5 rates, so it costs roughly what the original call's input cost (tens of cents for Call 1's large context, a few cents for Call 2). Logged as `decision_retry` in API tracker. Risk review parser also upgraded to use the full 5-strategy cascade.
- **analyst_guidance.md mount** — volume-mounted in `docker-compose.yml` (alongside `strategy.json`). Changes are reflected without rebuilding. If missing, Claude runs without hard rules — analysis quality degrades silently.
- **Pydantic validation** — all Claude JSON outputs validated via `AnalysisOutput`, `DecisionOutput`, `RiskReviewOutput` models. Graceful fallback on validation failure.
- **HTTP retry** — external data APIs (FRED, Alpha Vantage, EDGAR, Finnhub) wrapped with `retry_get`/`retry_call` (3 attempts, 1s/3s/5s backoff on transient errors only).
- **Timezone-aware datetimes** — all `datetime.utcnow()` replaced with `datetime.now(timezone.utc)`. For DB queries against `TIMESTAMP WITHOUT TIME ZONE` columns, use `.replace(tzinfo=None)` to avoid asyncpg comparison errors.
- **Trading day timezone** — NEVER use `date.today()` or `datetime.today()` in the backend. Always use `market_today()` or `market_now()` from `src/scorched/tz.py`. These return dates/times in NYSE timezone (`America/New_York`), ensuring the trading day is consistent regardless of server timezone. The Docker container also sets `TZ=America/New_York` as a safety net. The dashboard fetches the market date from `/api/v1/system/market-date` instead of using browser-local JS dates.
- **Trailing stops** — ATR-based trailing stops in `trailing_stops.py`. High-water mark ratchets up, stop follows at `hwm - 2*ATR` (or -5% floor). Initialized on buy in `apply_buy()`. Tracked in Position model (`trailing_stop_price`, `high_water_mark`). On every 5-min intraday tick: HWM is ratcheted up if price has risen, then `check_trailing_stop_breach()` fires a `trailing_stop_breached` trigger (6th intraday trigger) if current price has dropped below the stop. Breach triggers Claude exit evaluation, same as the other 5 triggers. New endpoint `POST /api/v1/portfolio/positions/{symbol}/trailing-stop` for manual stop adjustments (ratchet-only — can only move stop up).
- **Pre-market data** — `_fetch_premarket_prices_sync()` fetches pre-market/current prices via Alpaca snapshots (latest trade + previous daily bar close). Displayed per symbol in research context. Gap-up >5% flagged as chase risk. `check_gap_up_gate()` added to circuit breaker.
- **Weekly reflection** — `POST /api/v1/market/weekly-reflection` reviews past week's trades vs outcomes. `claude-opus-5` (effort `"medium"`) extracts learnings, patterns, and strategy adjustments. Appends to playbook. Cron: Sunday 6 PM ET.
- **Phase 0 cache location** — cache now writes to `/app/logs/` (Docker volume) instead of `/tmp` (ephemeral). Survives container restarts.
- **Strategy coherence** — `analyst_guidance.md` hard rule #3 updated to match `strategy.json` sector limits (40% max, not "never two in same sector"). ATR and relative strength interpretation guides added.
- **Twelvedata vs Alpha Vantage** — Twelvedata (800 calls/day) fetches RSI for ALL research symbols. Alpha Vantage (25 calls/day) only covers screener picks. Both are fetched in Phase 0; Twelvedata provides broader coverage. Both RSI values and the economic calendar are rendered directly into the `build_research_context()` output that Claude sees — not just fetched and discarded.
- **Crontab is hand-maintained** — the live crontab uses `CRON_TZ=America/New_York` with ET times directly: no DST re-runs ever needed. `scripts/setup_cron.py` generates the legacy UTC-offset format and REFUSES to install (it would append a duplicate schedule → double order submission). Edit with `crontab -e`; `setup_cron.py --check` / `--dry-run` still work for inspection.
- **Economic calendar** — uses FRED releases API (same key). Tracks CPI, Jobs, FOMC, GDP, PPI, PCE, retail sales, housing starts, consumer confidence, industrial production. Warns Claude about same-day releases.
- **MCP auth** — MCP mutation tools (`confirm_trade`, `reject_recommendation`, `get_recommendations`) require the `pin` parameter when `SETTINGS_PIN` is set. Read-only tools don't require a PIN. This matches REST endpoint behavior.
- **MCP confirm_trade routes through broker** — MCP `confirm_trade` uses `get_broker(db)` → `broker.submit_buy/sell()`, same as the REST endpoint. This ensures Alpaca orders are actually submitted (not just recorded in DB) regardless of whether the trade is confirmed via REST or MCP.
- **Momentum screener uses Alpaca bars** — `_fetch_momentum_screener_sync` calls `alpaca_data.fetch_bars_sync` for 90d of S&P 500 history (~5s vs yfinance's variable 60-500s). yfinance is a fallback if Alpaca returns nothing. Symbols are normalized `BRK-B` → `BRK.B` for Alpaca's dot-format tickers.
- **Alpaca bars bisect on bad symbols** — `fetch_bars_sync` catches "invalid symbol" / 422 errors and recursively splits the batch in half so a single bad ticker only loses itself instead of taking down 50 neighbors.
- **Phase 1 waits for Phase 0 cache** — if the cache is missing when Phase 1 starts (Phase 0 still running), Phase 1 polls up to 120s for it to appear before falling back to inline fetch. Prevents duplicate work when Phase 0 overruns.
- **Claude client is AsyncAnthropic with 300s timeout** — `claude_client._client()` returns `AsyncAnthropic` so the FastAPI event loop stays responsive during long LLM calls. `retry.py` works with both sync and async clients and retries on `APIStatusError` (5xx/429/529), `APITimeoutError`, and `APIConnectionError`.
- **Portfolio price fetch is Alpaca-first** — `_get_current_price` tries Alpaca snapshot first for equities/ETFs, yfinance only for index symbols (`^GSPC` etc.) or as fallback. Never raises — returns `Decimal("0")` on total failure.
- **Intraday VIX has VXX fallback** — `cron/intraday_monitor.py` fetches `^VIX` from yfinance; if that fails it uses the `VXX` ETF snapshot via Alpaca as a volatility proxy so the VIX market trigger stays armed.
- **Phase 0 gather timeout** — `asyncio.gather` over all data-source coroutines is wrapped with `asyncio.wait_for(..., timeout=PHASE0_GATHER_TIMEOUT_S)` (600s). If any source hangs past the deadline the whole gather cancels; a Telegram alert fires and Phase 0 returns whatever was collected before the hang. Prevents Phase 1 from waiting indefinitely for a Phase 0 that will never finish.
- **Phase 3 EOD timeout** — `http_post` in `cron/tradebot_phase3.py` uses `timeout=600` (Claude's own limit is 300s; playbook update adds another call on top). On failure (any exception), a Telegram alert is sent before re-raising.
- **`alpaca_live` boot refusal** — `main.py` checks at startup: if `BROKER_MODE=alpaca_live` and `SETTINGS_PIN` is unset or shorter than 16 chars, the process exits with a `RuntimeError`. Prevents accidentally going live with an unprotected mutation surface.
- **Sector gate is code-enforced** — `recommender.py` calls `check_sector_exposure()` for every proposed BUY. If the buy would push any sector above `strategy.json → concentration → max_sector_pct` (default 40%), the recommendation is dropped before saving, same as the drawdown gate. `analyst_guidance.md` hard rule #3 documents this; the code enforces it independently.
- **`total_value` uses live prices** — `portfolio.py` computes `total_value` (used by the position-size gate and drawdown gate) by fetching current prices via `_compute_portfolio_total_value()`, not cost basis. Prevents the gates from using stale book values when prices have moved significantly.
- **Phase timing alerts** — Phase 0 flags `⚠️ SLOW` in Telegram when runtime >400s; Phase 1 flags when >420s. Phase 1's cron HTTP timeout is 900s (raised from 600s) so server-side errors surface before the client's opaque `socket.timeout`.
- **Strategy PUT is a shallow merge, not a full overwrite** — `api/strategy.py:update_strategy()` merges the incoming form payload into the existing `strategy.json` rather than overwriting. The dashboard form only surfaces a subset of keys (`objective`, `concentration`, etc.); a full overwrite silently wiped safety sections (`circuit_breaker`, `intraday_monitor`, `drawdown_gate`) on every save. **If you add a new top-level section to strategy.json that isn't rendered by the dashboard form, it will be preserved across saves.**
- **Playbook drift guardrail** — `services/playbook.py:update_playbook()` injects the full `strategy.json` + the `## Hard Rules` section of `analyst_guidance.md` into the EOD update prompt and marks them immutable. After Claude responds, `_check_playbook_drift()` scans the output for known drift patterns (e.g., "10-day ceiling", "Tier 1 -3%", "+8% partial", "3-10 day holding", "7-day flat rule"). If any fire, the update is REJECTED, the prior playbook is preserved, a Telegram alert is sent, and the rejected payload is persisted to `/app/logs/playbook_rejections/{ts}_v{prior_version}.md` for forensics (added 2026-04-27). The playbook is for trade-level learnings — numeric rules live in `strategy.json` / `analyst_guidance.md` and cannot be rewritten from inside the playbook. **Whenever you change `strategy.json` or `analyst_guidance.md`, also update `_DRIFT_PATTERNS` if you tighten/loosen a rule whose old value would now count as drift. Avoid enumerating deprecated drift patterns verbatim in the playbook itself — Claude reads the playbook each session and parroting the patterns back can trigger false-positive rejections.**
- **Factor leadership + performance snapshot injection** — `build_research_context()` emits two new sections at the top of the Phase 1 context before any per-stock data: (1) `FACTOR LEADERSHIP` — 5-day and 20-day returns for SPY, QQQ, MTUM, SPMO, RSP, IWM from `fetch_factor_returns()` (Alpaca bars, same pattern as sector_returns), with a regime verdict when a non-SPY factor leads by ≥3 pts; (2) `PERFORMANCE vs BENCHMARKS` — portfolio return vs each benchmark + trade metrics (win rate, profit factor, expectancy, avg hold) from `get_benchmark_comparison()`. Both are cached in Phase 0 (`factor_returns` key). A matching hard rule #9 in `analyst_guidance.md` requires picks to align with the leading factor or cite a specific overriding catalyst.
- **GARCH(1,1) forward volatility** — `compute_technicals()` adds a `garch` block per symbol with `forward_annual_vol_pct` (5d ahead, annualized), `realized_annual_vol_pct` (20d realized), and a `regime` of `expanding` / `stable` / `contracting` (forecast/realized ratio bands at 1.15 and 0.85). Rendered next to ATR in `build_research_context()`. ATR is rolling/lagging; GARCH is conditional/forward-looking — they're complements. Requires the `arch` package (added to `pyproject.toml`).
- **Portfolio VaR / CVaR** — `services/risk.py` runs historical simulation over current holdings using Alpaca bars and current market-value weights. `GET /api/v1/portfolio/risk?confidence=0.95&lookback_days=252` returns `var_pct`, `cvar_pct`, `var_dollars`, `cvar_dollars`. Cash is treated as risk-free. Hard stops (-8%) and the drawdown gate are reactive; VaR/CVaR are forward-looking estimates of plausible single-day loss.
- **Backtester** — `services/backtest.py` has two modes that share `compute_metrics()` so results are directly comparable: (1) `replay_with_alternate_exits(entries, bars, stop_pct, target_pct, time_stop_days)` for re-running actual TradeHistory entries with different exit rules; (2) `simulate_breakout_strategy(bars, momentum_5d_min, volume_multiplier, rsi_min, rsi_max, ...)` for testing entry-rule edits. CLI driver at `scripts/backtest.py`. **Limitations**: no slippage/commission, daily bars only, single-thread compounding (no overlap modeling). Use it to compare *strategies*, not to project actual portfolio P&L.
- **Backtest before tuning `strategy.json` numerics** — before shipping any `strategy.json` numeric edit (stop, target, vol multiplier, RSI band, sector cap, drawdown trigger), run `scripts/backtest.py replay` (for exit changes) or `scripts/backtest.py sim` (for entry changes) against the current value vs the proposed value. Don't merge if expectancy goes negative or max drawdown deepens materially. The backtester is for *comparing* variants — use ranking, not absolute numbers.
- **Mean-reversion screener is code-gated by `entry_style`, not prompt-only** — `fetch_screeners()` in `services/research.py` runs `fetch_momentum_screener()` unconditionally but only calls `fetch_mean_reversion_screener()` when `"mean_reversion" in strategy.json["entry_style"]`. Both Phase 0 (`api/prefetch.py`) and the recommender's inline fallback (`services/recommender.py`) call this single shared helper so the gate can't drift out of sync between the two call sites. Previously (Experiment B, 2026-06-18) mean-reversion was suspended in `analyst_guidance.md` prose only — the screener kept running and kept feeding up to 10 candidates into Claude's context every session regardless. Re-enable by adding `"mean_reversion"` back to `strategy.json → entry_style` — no code change needed. When disabled, `mean_reversion_symbols` is written to the Phase 0 cache as `[]` (not omitted) — `build_research_context()` and the recommender's `cache.get("mean_reversion_symbols", [])` both handle the empty-list and omitted-key shapes identically.
- **`broker/sync` must never mirror a short** — `sync_positions()` has an explicit `broker_qty < 0` branch that logs, records an `unmanaged_short` correction (which the Phase 2.5 cron Telegram-alerts), and **skips** — a negative `Position` row would corrupt every downstream valuation and gate. There is also a defensive `local is None` branch. Before 2026-08-07 both cases fell through to the "qty differs" branch and ran `local.shares = ...` on `None`, raising `AttributeError` — that aborted the position loop *and* the cash reconciliation that follows it, so a single phantom Alpaca short silently stopped all daily reconciliation (2026-08-06: both the 10:45 and 14:00 syncs returned HTTP 500). Long-only strategy — a short is always either a broker data fault or unmanaged exposure, and the fix belongs at the broker, not in the local DB.
- **Equity history** — `equity_history` is one row per NYSE trading day (`snapshot_date` unique), written by `record_snapshot()` at the **top of `run_eod_review()`, before its no-session early return**, so it lands on every trading day. **Only when `review_date == market_today()`** — `record_snapshot()` reads *live* portfolio state, so a back-dated `POST /api/v1/market/eod-review?date=...` would stamp today's values onto a past date and, because `snapshot_date` is unique-with-upsert, silently overwrite a legitimate historical row. Back-dating the review is fine; back-dating the equity series is not. Read via `GET /api/v1/portfolio/equity-history?days=90` (oldest first; `days` caps **rows**, not calendar span — rows exist only for trading days). `record_snapshot()` swallows all its own errors and returns `None` — telemetry must never break Phase 3. `broker_equity` is NULL outside `alpaca_*` modes (PaperBroker reports cash as equity, which would be misleading stored next to `total_value`). **This exists because `portfolio` is a single row updated in place and reconstructing history from `trade_history` is unsafe** — see `.handovers/2026-08-06-performance-review.md` §2.
- **Exit telemetry columns** — `TradeHistory.exit_reason`/`exit_trigger` persist on every sell (`portfolio.py:apply_sell`, both broker adapters). `PendingFill` carries the same two columns as a pass-through during the fire-and-forget window, written at submit time and read back by `reconcile_pending_orders()` into `apply_sell()`. `exit_reason` is one of `"recommendation"` (a normal Phase 2 sell), `"intraday_hard_stop"`, `"intraday_claude_exit"`, or `"manual"` (reserved — no call site sets it yet); `exit_trigger` holds one of the 6 intraday trigger names when `exit_reason` is intraday-driven, else `NULL`. Telemetry-only — never changes execution (Alpaca fill data is still the economics source of truth); AlpacaBroker's pending-fill dedup path refreshes both columns on a resubmit so a client_order_id collision doesn't leave stale reasons.
- **Phase 2 is DB-authority, not file-authority** — `cron/tradebot_phase2.py` merges Phase 1's gated recommendations file with today's DB-pending recs (`fetch_db_pending_recs` + `merge_pending`); file entries win on duplicates (they carry gate results) but DB-only pending recs get added so a rec that reached the DB but not the file still executes. If the original (pre-gate) file is missing/unreadable/date-mismatched, the merge is skipped fail-closed with a Telegram alert (`db_merge_skip_reason`). If the gated file itself is entirely absent, Phase 2 falls back to a **DB-only rescue path** — executes DB-pending recs directly with no file at all. Expired/stale pending recs get cleaned up rather than executed indefinitely.
- **Re-entry cooldown gate** — `recommender.check_reentry_cooldown()` (hard rule #11, code-enforced) rejects a BUY if the same symbol has a SELL in `TradeHistory` within the last `reentry_cooldown_days` NYSE trading days (`strategy.json → reentry_cooldown_days`, default 3). NYSE trading-day math via `pandas_market_calendars`, not calendar days. Fails OPEN (allows the buy) on a DB error or malformed config — a config problem isn't evidence about the candidate.
- **Mechanical entry gate** — `recommender.check_mechanical_entry()` (hard rule #12, code-enforced) rejects any BUY that fails 5-day momentum > 0% (`week_change_pct`), relative volume ≥ 1.0x (`technicals[...]["volume"]["relative_volume"]`), or price above the 20-day MA (`technicals[...]["bollinger"]["middle"]`) — config under `strategy.json → mechanical_entry`. Fails CLOSED on missing data by default (`fail_open_on_missing: false`); shipped after a backtest showed LLM-originated entries losing to a mechanical breakout rule (Experiment B, 6/18). Rule #12 in `analyst_guidance.md` must keep the bold `**Title**` prefix (`N. **Title**: body`) — `services/guidance.py:_RULE_ENTRY_RE` silently absorbs any un-bolded numbered rule into the previous rule's body instead of erroring (fixed 2026-08-01 after Task 10 shipped it without the prefix).
- **Exposure advisory + hard rule #10** — `recommender.assess_exposure()` (Task 8) computes an advisory-only verdict (`underinvested` / `defensive_ok` / `ok` / `overinvested`) from `strategy.json → exposure.target_min_invested_pct`/`target_max_invested_pct`, gated by `regime_condition` (currently only `"spy_above_20dma_and_no_drawdown_gate"` is implemented — any other string is a false affordance, silently ignored). Writes a `gate_decisions` telemetry row every session but blocks nothing; the actual lever is `analyst_guidance.md` hard rule #10, which tells Claude it must either propose enough qualifying buys to close an `underinvested` gap or name, per vacancy, which entry criterion the best remaining candidate failed. Ceiling side (`overinvested`) stays enforced by the existing position/cash/holdings/sector gates.
- **`trailing_stop` config section** — `strategy.json → trailing_stop` (`atr_multiplier: 2.0`, `floor_pct: 5.0`) now drives `trailing_stops.py`'s HWM ratchet instead of hardcoded constants — stop follows at `hwm - atr_multiplier*ATR`, floored at `-floor_pct%`. Change here, not in code, to retune the trailing-stop-only exit policy (Experiment B/C both run `sell_discipline: "trailing_stop"`, no fixed profit targets, `partial_sell: never`).
- **Experiment C (active)** — `strategy.json → experiment` block: started 2026-08-03, `deadline_approx_date` 2026-10-27 (60 NYSE trading sessions, real calendar lookup via `pandas_market_calendars`, not approximate week math like Experiment B's). Kill criterion: retire unless window return beats SPY's window return AND window profit factor > 1.0. Carries forward B's breakout-only/15%-cap/factor-gate/trailing-stop-only policy and adds the mechanical entry gate, re-entry cooldown, and exposure discipline above. Evaluate with `docker compose exec -T tradebot python3 scripts/evaluate_experiment.py` (config-driven off `strategy.json → experiment`, generalized from the old `evaluate_experiment_b.py` so future resets are a config edit, not a script edit) — measures and reports only, never mutates strategy or stops crons.

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

Optional (timezone — rarely needed):
```
MARKET_TIMEZONE=America/New_York   # Default. Controls trading day boundaries (NYSE time).
                                    # Do NOT change for user display preferences — this is market time.
                                    # Only change if trading on a non-US exchange (e.g., Asia/Tokyo for TSE).
```

Optional (data sources):
```
TWELVEDATA_API_KEY=           # Free tier: 800 calls/day. RSI for full watchlist.
```

`DATABASE_URL` is injected by Docker Compose. Only needed for local non-Docker runs:
```
DATABASE_URL=postgresql+asyncpg://scorched:scorched@localhost:5432/scorched
```
