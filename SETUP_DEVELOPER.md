> **Note:** This guide is for developers setting up a local development environment. If you just want to run the bot, see [START_HERE.md](START_HERE.md) instead.

---

# Scorched AI Trading Bot — Setup Guide

A Claude-powered stock trading bot you can talk to in plain English. Generates daily trade recommendations using fundamental analysis, technical signals, and macro data, then executes them automatically. Connect Claude Desktop to ask your bot questions, review its picks, and understand its reasoning — no trading or coding knowledge required.

## What It Does

Every trading day:
1. **9:35 AM ET** — Phase 0: post-open data prefetch (live opening gaps + volume, zero LLM cost)
2. **9:45 AM ET** — Phase 1: Claude analyzes ~80 stocks using 7+ data sources and generates up to 3 trade recommendations
3. **9:55 AM ET** — Phase 1.5: circuit breaker checks if the market or individual stocks have moved against the thesis (25 min of live data)
4. **10:15 AM ET** — Phase 2: approved trades are submitted to the broker (post opening-range volatility window)
5. **10:45 AM ET** — Phase 2.5: reconcile pending Alpaca fills, sync positions against the broker as source of truth
6. **9:35 AM–3:55 PM ET** — Intraday monitor checks positions every 5 minutes against 5 triggers (position drop, SPY drop, VIX spike, volume surge); Claude evaluates exits only when triggered
7. **4:01 PM ET** — Phase 3: end-of-day review — Claude compares morning thesis vs. actual outcomes and updates its playbook

The bot learns from its trades via a living playbook that carries lessons forward to future decisions.

## Prerequisites

- A server or VM (Ubuntu recommended) with Docker installed
- An Anthropic API key (Claude API access)
- Optional: Alpaca account for paper/live trading
- Recommended: Alpaca API key (free) — primary market data source; same key powers paper/live trading
- Optional: API keys for FRED, Twelvedata, Alpha Vantage, Finnhub (enhances analysis but not required)

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/willcassell/scorched.git
cd scorched
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env` and fill in at minimum:

```
ANTHROPIC_API_KEY=sk-ant-api03-your-key-here
```

That's the only required key. Everything else has sensible defaults.

### 3. Start the bot

```bash
docker compose up -d --build
```

Wait ~2 minutes for the build, then verify:

```bash
curl http://localhost:8000/health
# Should return: {"status":"ok","db":"connected"}
```

### 4. Open the dashboard

Visit `http://your-server-ip:8000` in a browser. You'll see the trading dashboard with portfolio status, positions, and trade history.

Visit `http://your-server-ip:8000/strategy` to configure the trading strategy (hold period, position sizing, risk guardrails, etc.).

### 5. Connect Claude Desktop (talk to your bot)

Open Claude Desktop → Settings → Developer → Edit Config, and add:

```json
{
  "mcpServers": {
    "tradebot": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

Replace `localhost` with your server IP if running on a VM. Restart Claude Desktop.

Now ask questions in natural language:
- *"What stocks did you recommend today?"*
- *"How's the portfolio doing?"*
- *"Show me the market summary"*
- *"What does your playbook say?"*

Claude calls the bot's 7 MCP tools automatically — you never need to learn API syntax or trading commands.

### 6. Set up the daily cron jobs

```bash
crontab -e
```

Add these lines. **Times are in ET** (NYSE local time). The Docker container sets `TZ=America/New_York`; if your VM is UTC, run `python3 scripts/setup_cron.py` instead — it computes the right host-local times for you.

```cron
# Scorched AI Trading Bot — Daily Cycle (times in ET)

# Phase 0: Post-open data prefetch (zero LLM cost)
35 9 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase0.py >> logs/cron.log 2>&1

# Phase 1: Claude analysis + recommendations (uses real opening data)
45 9 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase1.py >> logs/cron.log 2>&1

# Phase 1.5: Circuit breaker gate (25 min of live data)
55 9 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase1_5.py >> logs/cron.log 2>&1

# Phase 2: Submit orders post opening-range
15 10 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase2.py >> logs/cron.log 2>&1

# Phase 2.5: Reconcile Alpaca fills + sync positions
45 10 * * 1-5 cd ~/scorched && python3 cron/tradebot_reconcile.py >> logs/cron.log 2>&1

# Intraday: Position monitoring (9:35 AM–3:55 PM ET, self-gates to market hours)
*/5 9-15 * * 1-5 cd ~/scorched && python3 cron/intraday_monitor.py >> logs/cron.log 2>&1

# Phase 3: End-of-day summary
01 16 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase3.py >> logs/cron.log 2>&1
```

The cron scripts need `pytz` installed on the host (not just in Docker):
```bash
pip3 install pytz
```

### 6. (Optional) Set up Telegram notifications

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather) — you'll get a bot token
2. Send a message to your bot, then get your chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=your-bot-token
   TELEGRAM_CHAT_ID=your-chat-id
   ```

The cron scripts also need these env vars. Create a file the cron can source:
```bash
cat > ~/.tradebot_cron_env << 'EOF'
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=your-chat-id
EOF
```

Then update crontab entries to source it:
```cron
30 8 * * 1-5 set -a; . ~/.tradebot_cron_env; set +a; python3 ~/scorched/cron/tradebot_phase1.py >> ~/scorched/logs/cron.log 2>&1
```

### 7. (Optional) Connect Alpaca for broker execution

By default, trades are paper-only (tracked in the database, no real orders). To use Alpaca:

1. Create a free account at [alpaca.markets](https://alpaca.markets)
2. Go to Paper Trading > API Keys > Generate New Key
3. Add to `.env`:
   ```
   BROKER_MODE=alpaca_paper
   ALPACA_API_KEY=PKXXXXXXXXXXXXXXXXXX
   ALPACA_SECRET_KEY=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
   ```
4. Rebuild: `docker compose up -d --build --force-recreate tradebot`

## API Keys — What You Need

### Required

| Key | What it does | How to get it |
|-----|-------------|---------------|
| `ANTHROPIC_API_KEY` | Powers Claude analysis and trade decisions | Sign up at [console.anthropic.com](https://console.anthropic.com), create an API key. Costs ~$0.02-0.05/day. |

### Strongly Recommended

These are free and significantly improve analysis quality. Without them, Claude is making decisions with less data.

| Key | What you lose without it | How to get it |
|-----|--------------------------|---------------|
| `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` | Primary market data — prices, snapshots, news, screener. Without these the bot has almost no per-stock data. | Free at [alpaca.markets](https://alpaca.markets) → Paper Trading → API Keys. Free IEX feed is enough. |
| `FRED_API_KEY` | No macro data — Fed funds rate, Treasury yields, CPI, unemployment, credit spreads, economic calendar. Claude flies blind on macro. | Free — sign up at [fred.stlouisfed.org/docs/api/api_key.html](https://fred.stlouisfed.org/docs/api/api_key.html). Takes 2 minutes. |
| `TWELVEDATA_API_KEY` | No RSI for the full watchlist — only the ~20 screener picks get RSI via Alpha Vantage fallback. | Free (800 calls/day) at [twelvedata.com](https://twelvedata.com) → Account → API Keys. |
| `ALPHA_VANTAGE_API_KEY` | No RSI fallback for the momentum screener picks. | Free (25 calls/day) at [alphavantage.co/support/#api-key](https://www.alphavantage.co/support/#api-key) |
| `FINNHUB_API_KEY` | No analyst consensus / recommendation trend data. | Free at [finnhub.io](https://finnhub.io) |

> **Polygon.io was removed in April 2026** — Alpaca news now provides headlines and summaries on the same key as the broker.

### Optional (for broker execution)

| Key | What it does | How to get it |
|-----|-------------|---------------|
| `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` | Executes trades on Alpaca (paper or live) instead of DB-only tracking | Free at [alpaca.markets](https://alpaca.markets) — sign up, go to Paper Trading > API Keys > Generate |

Without Alpaca keys, the bot runs in paper-only mode (tracks everything in the database, no real orders). This is perfectly fine for evaluation.

### Optional (for notifications)

| Key | What it does | How to get it |
|-----|-------------|---------------|
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Sends trade alerts and daily summaries to your phone | Create a bot via [@BotFather](https://t.me/BotFather) on Telegram |

**Bottom line:** To get the full experience, you want all the free API keys (Anthropic, Alpaca, FRED, Twelvedata, Alpha Vantage, Finnhub). The whole setup takes about 10 minutes. Without the data keys the bot still runs, but Claude is trading with one hand tied behind its back.

## Configuration

### Strategy Settings

Edit via the dashboard at `/strategy` or directly in `strategy.json`:

- **hold_period**: How long to hold positions (current: `2-6wk` swing/position)
- **concentration.max_holdings**: Maximum simultaneous positions (current: 10; code default fallback: 5)
- **concentration.max_position_pct**: Max % of portfolio per position (current: 33; code default fallback: 20)
- **concentration.max_sector_pct**: Max % of portfolio in a single sector (current: 40, code-enforced)
- **loss_management**: `time_price_hybrid` — combined -8% hard stop and 30-day time stop
- **circuit_breaker**: Gate thresholds for blocking buys into falling markets (gap-down, SPY drop, VIX)
- **drawdown_gate**: Blocks new buys when portfolio drops >8% from peak equity

### Portfolio

- `STARTING_CAPITAL` in `.env` sets the initial portfolio value (default: $100,000)
- The portfolio is seeded on first startup and persists in PostgreSQL

## Useful Commands

```bash
# View logs
docker compose logs tradebot -f

# Rebuild after code changes
docker compose up -d --build --force-recreate tradebot

# Check portfolio via API
curl http://localhost:8000/api/v1/portfolio | python3 -m json.tool

# Check broker status
curl http://localhost:8000/api/v1/broker/status | python3 -m json.tool

# Manually trigger recommendations (outside cron)
curl -X POST http://localhost:8000/api/v1/recommendations/generate

# Run database migrations manually
docker compose exec tradebot alembic upgrade head
```

## Architecture

```
    You (Claude Desktop)          Cron (automated daily cycle)
           |                              |
     "How's my portfolio?"          Scheduled triggers
           |                              |
           ▼                              ▼
      MCP (/mcp)                    REST (/api/v1)
           \                          /
            ▼                        ▼
          FastAPI app (scorched)
                  |
         Claude API (analysis + decisions)
                  |
            Broker Adapter
             /        \
          Paper      Alpaca
          (DB)     (real orders)
                  |
             PostgreSQL
```

**Two ways in, same brain.** The MCP server and REST API call the same logic. Claude Desktop users talk to the bot in natural language; cron jobs hit the REST endpoints on a schedule. Both paths feed the same portfolio, the same Claude pipeline, and the same database.

The bot uses a multi-call Claude pipeline:
1. **Analysis call** (extended thinking) — reviews all market data, identifies candidates
2. **Decision call** — takes the analysis + current portfolio and outputs specific trades
3. **Risk review** — adversarial review that challenges and rejects weak picks
4. **Position management** — EOD review of all open positions

Data sources: Alpaca Data API (prices/snapshots/news/screener), yfinance (fundamentals/options/insider/indices), FRED (macro + economic calendar), Twelvedata + Alpha Vantage (RSI), SEC EDGAR (Form 4), Finnhub (analyst consensus).

## Troubleshooting

**Container won't start:** Check `docker compose logs tradebot` — usually a missing `ANTHROPIC_API_KEY` or migration error.

**No recommendations generated:** Check if the market is open (NYSE holidays are detected automatically). Check `logs/cron.log` for errors.

**Telegram not sending:** Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in both `.env` and the cron env file.

**Port 8000 not accessible:** Check firewall: `sudo ufw allow 8000/tcp`
