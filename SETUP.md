# Scorched AI Trading Bot — Setup Guide

A Claude-powered stock trading bot that generates daily trade recommendations using fundamental analysis, technical signals, and macro data, then executes them automatically.

## What It Does

Every trading day:
1. **8:30 AM ET** — Claude analyzes 40 stocks using 7 data sources and generates up to 3 trade recommendations
2. **9:30 AM ET** — Circuit breaker checks if the market or individual stocks have moved against the thesis
3. **9:35 AM ET** — Approved trades are executed (paper or via Alpaca broker)
4. **4:01 PM ET** — End-of-day review: Claude compares morning thesis vs. actual outcomes and updates its playbook

The bot learns from its trades via a living playbook that carries lessons forward to future decisions.

## Prerequisites

- A server or VM (Ubuntu recommended) with Docker installed
- An Anthropic API key (Claude API access)
- Optional: Alpaca account for paper/live trading
- Optional: API keys for FRED, Polygon, Alpha Vantage (enhances analysis but not required)

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

### 5. Set up the daily cron jobs

```bash
crontab -e
```

Add these lines (adjust timezone handling for your server):

```cron
# Scorched AI Trading Bot — Daily Cycle (times in ET)
# If your server is UTC, convert: 8:30 AM ET = 12:30 UTC (DST) or 13:30 UTC (EST)

# Phase 1: Pre-market recommendations
30 8 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase1.py >> logs/cron.log 2>&1

# Phase 1.5: Circuit breaker gate check
30 9 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase1_5.py >> logs/cron.log 2>&1

# Phase 2: Execute trades
35 9 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase2.py >> logs/cron.log 2>&1

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
| `FRED_API_KEY` | No macro data — Fed funds rate, Treasury yields, CPI, unemployment, credit spreads. Claude flies blind on macro. | Free — sign up at [fred.stlouisfed.org/docs/api/api_key.html](https://fred.stlouisfed.org/docs/api/api_key.html). Takes 2 minutes. |
| `POLYGON_API_KEY` | Falls back to yfinance news (lower quality, less timely headlines) | Free tier at [polygon.io](https://polygon.io). Sign up, key is on your dashboard. |
| `ALPHA_VANTAGE_API_KEY` | No RSI data for the momentum screener — screening still works but without a key technical signal | Free (25 calls/day) at [alphavantage.co/support/#api-key](https://www.alphavantage.co/support/#api-key) |

### Optional (for broker execution)

| Key | What it does | How to get it |
|-----|-------------|---------------|
| `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` | Executes trades on Alpaca (paper or live) instead of DB-only tracking | Free at [alpaca.markets](https://alpaca.markets) — sign up, go to Paper Trading > API Keys > Generate |

Without Alpaca keys, the bot runs in paper-only mode (tracks everything in the database, no real orders). This is perfectly fine for evaluation.

### Optional (for notifications)

| Key | What it does | How to get it |
|-----|-------------|---------------|
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Sends trade alerts and daily summaries to your phone | Create a bot via [@BotFather](https://t.me/BotFather) on Telegram |

**Bottom line:** To get the full experience, you want all four free API keys (Anthropic, FRED, Polygon, Alpha Vantage). The whole setup takes about 10 minutes. Without the data keys the bot still runs, but Claude is trading with one hand tied behind its back.

## Configuration

### Strategy Settings

Edit via the dashboard at `/strategy` or directly in `strategy.json`:

- **hold_period**: How long to hold positions (default: "3-10d")
- **concentration.max_holdings**: Maximum simultaneous positions (default: 5)
- **concentration.max_position_pct**: Max % of portfolio per position (default: 20)
- **loss_management**: "hard_stop" enables automatic stop-loss exits
- **circuit_breaker**: Gate thresholds for blocking buys into falling markets

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
Claude API (analysis + decisions)
        |
   FastAPI app (scorched)
    /         \
  REST       MCP
  /api/v1    /mcp
    |
  Broker Adapter
   /        \
Paper    Alpaca
(DB)     (real orders)
    |
PostgreSQL
```

The bot uses a two-call Claude pipeline:
1. **Analysis call** (extended thinking) — reviews all market data, identifies candidates
2. **Decision call** — takes the analysis + current portfolio and outputs specific trades

Data sources: yfinance, FRED, Polygon, Alpha Vantage, SEC EDGAR, internal momentum screener, options data.

## Troubleshooting

**Container won't start:** Check `docker compose logs tradebot` — usually a missing `ANTHROPIC_API_KEY` or migration error.

**No recommendations generated:** Check if the market is open (NYSE holidays are detected automatically). Check `logs/cron.log` for errors.

**Telegram not sending:** Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in both `.env` and the cron env file.

**Port 8000 not accessible:** Check firewall: `sudo ufw allow 8000/tcp`
