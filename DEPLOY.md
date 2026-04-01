# Tradebot Deployment Guide

## 1. Files to Copy to Your VM

Copy the entire project directory using rsync with your SSH key:

```bash
# Fix key permissions first (required — SSH refuses keys that are world-readable)
chmod 600 /path/to/your-ssh-key.key

# Sync project to VM (run from your local machine)
rsync -av \
  -e "ssh -i /path/to/your-ssh-key.key" \
  --exclude='.venv' \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.env' \
  /path/to/tradebot/ \
  ubuntu@YOUR_VM_IP:~/tradebot/
```

For subsequent deploys (after code changes), the same command is safe to re-run — rsync only transfers files that have changed.

To SSH into the VM directly:

```bash
ssh -i /path/to/your-ssh-key.key ubuntu@YOUR_VM_IP
```

**Key files that must be present on the VM:**

```
tradebot/
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh
├── pyproject.toml
├── alembic.ini
├── strategy.json
├── analyst_guidance.md
├── .env                  ← you create this (see Section 3)
├── alembic/              ← database migrations
├── cron/                 ← daily automation scripts
│   ├── common.py
│   ├── tradebot_phase0.py
│   ├── tradebot_phase1.py
│   ├── tradebot_phase1_5.py
│   ├── tradebot_phase2.py
│   ├── tradebot_phase3.py
│   └── intraday_monitor.py
└── src/scorched/         ← main application
    ├── main.py
    ├── config.py
    ├── models.py
    ├── prompts/          ← Claude system prompts
    ├── api/              ← REST endpoints
    ├── broker/           ← Alpaca / paper broker
    ├── services/         ← business logic
    └── static/           ← dashboard HTML
```

---

## 2. Building and Running the Docker Container

### First-time setup on the VM

```bash
cd ~/tradebot

# Create your .env file (see Section 3 below)
cp .env.example .env
nano .env   # fill in your API key and settings

# Build and start everything
docker compose up -d --build
```

This will:
1. Pull `postgres:16` and `python:3.11-slim` images
2. Build the tradebot image
3. Start Postgres, wait for it to be healthy
4. Start tradebot, run Alembic migrations, then start the server

### Verify it's running

```bash
# Check both containers are up
docker compose ps

# Check tradebot logs
docker compose logs tradebot --tail=50

# Hit the health endpoint
curl http://localhost:8000/health
# Expected: {"status":"ok","db":"connected"}

# View the dashboard
# Open http://your-vm-ip:8000 in a browser
```

### Subsequent deploys (after code changes)

```bash
cd ~/tradebot

# Pull updated files (rsync from Mac, see Section 1)

# Rebuild and restart tradebot only (keeps postgres data)
docker compose up -d --build tradebot

# Watch logs
docker compose logs tradebot -f
```

### Useful commands

```bash
# Stop everything
docker compose down

# Stop but keep the postgres data volume
docker compose stop

# Wipe everything including the database (destructive!)
docker compose down -v

# Shell into the tradebot container
docker compose exec tradebot sh

# Shell into postgres
docker compose exec postgres psql -U scorched scorched
```

---

## 3. Settings and API Keys

Create `~/tradebot/.env` on your VM with these values:

```bash
# ── REQUIRED ──────────────────────────────────────────────────────────────────

# Your Anthropic API key (from console.anthropic.com)
ANTHROPIC_API_KEY=sk-ant-api03-...

# ── PORTFOLIO ─────────────────────────────────────────────────────────────────

# Starting capital in dollars (no commas)
STARTING_CAPITAL=100000

# ── TAX RATES (optional — these are the defaults) ─────────────────────────────

SHORT_TERM_TAX_RATE=0.37
LONG_TERM_TAX_RATE=0.20

# ── SERVER ────────────────────────────────────────────────────────────────────

PORT=8000
HOST=0.0.0.0

# ── OPTIONAL DATA SOURCES ─────────────────────────────────────────────────────

# FRED (Federal Reserve Economic Data) — free, enables macro indicators
# (Fed Funds Rate, yield curve, CPI, unemployment, PCE, credit spreads)
# Get a key in 60 seconds:
#   1. Register at https://fredaccount.stlouisfed.org/login/secure/
#   2. My Account → API Keys → Request API Key (instant approval)
FRED_API_KEY=

# Alpha Vantage — free tier 25 calls/day; used for RSI(14) on screener picks only
# https://www.alphavantage.co/support/#api-key
ALPHA_VANTAGE_API_KEY=

# Polygon.io — free tier; used for better news headlines
# https://polygon.io/dashboard/signup
POLYGON_API_KEY=

# ── OPTIONAL SECURITY ─────────────────────────────────────────────────────────

# If set, PUT /api/v1/strategy (dashboard settings save) requires this PIN
SETTINGS_PIN=
```

**Note:** The `DATABASE_URL` is NOT needed in `.env` — `docker-compose.yml` sets it
automatically to point to the internal Postgres container. If you ever run the server
outside Docker, you'd need: `DATABASE_URL=postgresql+asyncpg://scorched:scorched@localhost:5432/scorched`

### Firewall / port access

If your VM has a firewall, open port 8000:

```bash
# Ubuntu/Debian with ufw
sudo ufw allow 8000/tcp

# Or restrict to specific IPs only (recommended)
sudo ufw allow from YOUR_IP to any port 8000
```

---

## 4. Cron Setup (Automated Daily Cycle)

The daily trading cycle is driven entirely by cron jobs on the VM. No AI orchestrator or external service is needed.

### Edit the crontab

```bash
crontab -e
```

Add these lines (all times are UTC — see DST table below):

```cron
# ── Tradebot daily cycle (times in UTC, after DST) ──────────────────────────

# Phase 0: Data prefetch — all external APIs, zero LLM cost (7:30 AM ET = 11:30 UTC)
30 11 * * 1-5 cd ~/tradebot && python3 cron/tradebot_phase0.py >> ~/tradebot/cron.log 2>&1

# Phase 1: Claude analysis + recommendations, loads Phase 0 cache (8:30 AM ET = 12:30 UTC)
30 12 * * 1-5 cd ~/tradebot && python3 cron/tradebot_phase1.py >> ~/tradebot/cron.log 2>&1

# Phase 1.5: Circuit breaker gate (9:30 AM ET = 13:30 UTC)
30 13 * * 1-5 cd ~/tradebot && python3 cron/tradebot_phase1_5.py >> ~/tradebot/cron.log 2>&1

# Phase 2: Confirm trades at opening prices (9:35 AM ET = 13:35 UTC)
35 13 * * 1-5 cd ~/tradebot && python3 cron/tradebot_phase2.py >> ~/tradebot/cron.log 2>&1

# Intraday: Position monitoring with trigger-based exit evaluation (9:35 AM–3:55 PM ET, self-gates)
*/5 13-19 * * 1-5 cd ~/tradebot && python3 cron/intraday_monitor.py >> ~/tradebot/cron.log 2>&1

# Phase 3: EOD summary + playbook update (4:01 PM ET = 20:01 UTC)
01 20 * * 1-5 cd ~/tradebot && python3 cron/tradebot_phase3.py >> ~/tradebot/cron.log 2>&1
```

### What Each Phase Does

| Phase | Time (ET) | Script | Action |
|-------|-----------|--------|--------|
| 0 | 7:30 AM | `cron/tradebot_phase0.py` | Prefetches all external market data, caches for Phase 1. Zero LLM cost. |
| 1 | 8:30 AM | `cron/tradebot_phase1.py` | Loads Phase 0 cache, runs Claude analysis + recommendations |
| 1.5 | 9:30 AM | `cron/tradebot_phase1_5.py` | Circuit breaker — blocks buys that fail safety checks |
| 2 | 9:35 AM | `cron/tradebot_phase2.py` | Confirms trades at opening prices via broker |
| Intraday | 9:35 AM–3:55 PM | `cron/intraday_monitor.py` | Every 5 min — checks positions against triggers, calls Claude for exit decisions if any fire |
| 3 | 4:01 PM | `cron/tradebot_phase3.py` | EOD review, playbook update, summary notification |

All scripts read `.env` from the project root automatically and send results via Telegram (if configured).

### DST time adjustments

The cron jobs use UTC. US Eastern Time shifts twice a year:

| Period | ET offset | Phase 0 (7:30 AM ET) | Phase 1 (8:30 AM ET) | Phase 2 (9:35 AM ET) |
|--------|-----------|----------------------|----------------------|----------------------|
| After DST (~Mar 8) | UTC-4 | `30 11 * * 1-5` | `30 12 * * 1-5` | `35 13 * * 1-5` |
| Before DST (~Nov 1) | UTC-5 | `30 12 * * 1-5` | `30 13 * * 1-5` | `35 14 * * 1-5` |

Update the crontab on the VM each time US clocks change.

### Manual triggers

```bash
# Force a fresh recommendation run (bypass cache)
curl -s -X POST http://localhost:8000/api/v1/recommendations/generate \
  -H "Content-Type: application/json" \
  -d '{"force": true}'

# Get portfolio state
curl -s http://localhost:8000/api/v1/portfolio | python3 -m json.tool

# Get EOD market summary (after 4 PM ET)
curl -s http://localhost:8000/api/v1/market/summary | python3 -m json.tool
```

---

## Dashboard

Once running, open a browser to:

```
http://YOUR_VM_IP:8000
```

You'll see:
- **Left column**: Portfolio performance, today's trade picks, token cost tracker (with $1/day progress bar)
- **Center**: Open positions with buy thesis, today's market analysis (with extended thinking toggle), recent closed trades
- **Right column**: Tax summary (ST/LT breakdown), living strategy playbook, strategy settings editor

The dashboard auto-refreshes every 5 minutes.

---

## Talk to Your Bot (MCP Server)

Once deployed, you can **talk to your bot in plain English** from Claude Desktop or any MCP client. The bot runs a Streamable HTTP MCP server at `http://YOUR_VM_IP:8000/mcp` with 7 tools that expose the full trading system.

### Connect Claude Desktop

On your local machine (not the VM), open Claude Desktop → Settings → Developer → Edit Config:

```json
{
  "mcpServers": {
    "tradebot": {
      "url": "http://YOUR_VM_IP:8000/mcp"
    }
  }
}
```

Restart Claude Desktop. Now you can ask questions like:

- *"What did you buy today and why?"*
- *"How's the portfolio doing?"*
- *"Run today's analysis — what looks good?"*
- *"What does the playbook say about what's been working?"*
- *"Show me how the market did today"*

Claude calls the right tools automatically and explains everything in plain English. No API knowledge or trading commands needed.

### Available tools

| Tool | What it does |
|------|-------------|
| `get_recommendations` | Trigger the full research + Claude analysis pipeline. Returns up to 3 trade picks. |
| `get_opening_prices` | Fetch actual opening auction prices for any list of symbols. |
| `confirm_trade` | Execute a recommended trade — updates portfolio, tracks P&L and taxes. |
| `reject_recommendation` | Skip a pick while keeping the audit trail clean. |
| `get_portfolio` | Live snapshot — positions, unrealized P&L, cash balance, tax classification. |
| `get_market_summary` | End-of-day performance for major indices + all S&P 500 sector ETFs. |
| `read_playbook` | Read the bot's living strategy doc — accumulated lessons from past trades. |

### Firewall note

Make sure port 8000 is open on your VM (see the Firewall section above). Claude Desktop on your local machine connects to the VM over HTTP — the same port that serves the dashboard.
