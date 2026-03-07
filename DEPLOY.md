# Tradebot Deployment Guide

## 1. Files to Copy to Your VM

Copy the entire project directory using rsync with your SSH key:

```bash
# Fix key permissions first (required — SSH refuses keys that are world-readable)
chmod 600 ~/Downloads/ssh-key-2026-02-19-private.key

# Sync project to VM (run from your Mac)
rsync -av \
  -e "ssh -i ~/Downloads/ssh-key-2026-02-19-private.key" \
  --exclude='.venv' \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.env' \
  ~/Projects/claude/tradebot/ \
  ubuntu@129.213.37.242:~/tradebot/
```

For subsequent deploys (after code changes), the same command is safe to re-run — rsync only transfers files that have changed.

To SSH into the VM directly:

```bash
ssh -i ~/Downloads/ssh-key-2026-02-19-private.key ubuntu@129.213.37.242
```

**Files that must be present on the VM:**

```
tradebot/
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh
├── pyproject.toml
├── alembic.ini
├── .env                  ← you create this (see Section 3)
├── alembic/
│   ├── env.py
│   └── versions/
└── src/
    └── scorched/
        ├── __init__.py
        ├── main.py
        ├── config.py
        ├── cost.py
        ├── database.py
        ├── models.py
        ├── schemas.py
        ├── mcp_tools.py
        ├── tax.py
        ├── static/
        │   └── dashboard.html
        ├── api/
        │   ├── __init__.py
        │   ├── costs.py
        │   ├── market.py
        │   ├── playbook.py
        │   ├── portfolio.py
        │   ├── recommendations.py
        │   ├── strategy.py
        │   └── trades.py
        └── services/
            ├── __init__.py
            ├── playbook.py
            ├── portfolio.py
            ├── recommender.py
            ├── research.py
            └── strategy.py
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

Add these lines:

```cron
# ── Tradebot daily cycle (times in UTC) ──────────────────────────────────────

# Phase 1: Pre-market research + recommendations (8:30 AM ET)
# After DST (Mar 8 – Nov 1):   30 12 * * 1-5
# Before DST (Nov 1 – Mar 8):  30 13 * * 1-5
30 12 * * 1-5 curl -s -X POST http://localhost:8000/api/v1/recommendations/generate -H "Content-Type: application/json" -d '{}' >> /home/ubuntu/tradebot/cron.log 2>&1

# Phase 2: Confirm trades at opening prices (9:45 AM ET)
# After DST:   45 13 * * 1-5
# Before DST:  45 14 * * 1-5
45 13 * * 1-5 /home/ubuntu/tradebot/scripts/confirm_trades.sh >> /home/ubuntu/tradebot/cron.log 2>&1
```

### Phase 1: Recommendation generation

The Phase 1 cron directly POSTs to the recommendations endpoint:

```bash
curl -s -X POST http://localhost:8000/api/v1/recommendations/generate \
  -H "Content-Type: application/json" \
  -d '{}'
```

The response includes the session_id and all pending recommendation IDs with suggested prices.

### Phase 2: Trade confirmation script

Create `/home/ubuntu/tradebot/scripts/confirm_trades.sh`:

```bash
#!/bin/bash
# Fetch today's pending recommendations and confirm each at the opening price.

BASE="http://localhost:8000/api/v1"

# Get today's pending recommendations
RECS=$(curl -s "$BASE/recommendations" | python3 -c "
import sys, json
data = json.load(sys.stdin)
# Get today's session
sessions = data if isinstance(data, list) else data.get('sessions', [])
today = sessions[0] if sessions else {}
recs = today.get('recommendations', [])
for r in recs:
    if r.get('status') == 'pending':
        print(r['id'], r['symbol'])
" 2>/dev/null)

if [ -z "$RECS" ]; then
    echo "$(date): No pending recommendations found."
    exit 0
fi

# For each pending rec, fetch the opening price and confirm
while IFS=' ' read -r REC_ID SYMBOL; do
    # Fetch opening price for this symbol
    OPEN_PRICE=$(curl -s -X POST "$BASE/trades/opening-prices" \
      -H "Content-Type: application/json" \
      -d "{\"symbols\": [\"$SYMBOL\"]}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
prices = data.get('opening_prices', {})
p = prices.get('$SYMBOL')
print(p if p else '')
" 2>/dev/null)

    if [ -z "$OPEN_PRICE" ] || [ "$OPEN_PRICE" = "None" ]; then
        echo "$(date): No opening price for $SYMBOL — skipping $REC_ID"
        continue
    fi

    # Confirm the trade at the opening price
    curl -s -X POST "$BASE/trades/confirm" \
      -H "Content-Type: application/json" \
      -d "{\"recommendation_id\": $REC_ID, \"execution_price\": $OPEN_PRICE}" \
      && echo "$(date): Confirmed $SYMBOL (rec $REC_ID) @ \$$OPEN_PRICE"

done <<< "$RECS"
```

```bash
chmod +x /home/ubuntu/tradebot/scripts/confirm_trades.sh
mkdir -p /home/ubuntu/tradebot/scripts
```

### DST time adjustments

The cron jobs use UTC. US Eastern Time shifts twice a year:

| Period | ET offset | Phase 1 (8:30 AM ET) | Phase 2 (9:45 AM ET) |
|--------|-----------|----------------------|----------------------|
| After DST (~Mar 8) | UTC-4 | `30 12 * * 1-5` | `45 13 * * 1-5` |
| Before DST (~Nov 1) | UTC-5 | `30 13 * * 1-5` | `45 14 * * 1-5` |

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
http://129.213.37.242:8000
```

You'll see:
- **Left column**: Portfolio performance, today's trade picks, token cost tracker (with $1/day progress bar)
- **Center**: Open positions with buy thesis, today's market analysis (with extended thinking toggle), recent closed trades
- **Right column**: Tax summary (ST/LT breakdown), living strategy playbook, strategy settings editor

The dashboard auto-refreshes every 5 minutes.

Tailscale address for remote access: `http://100.69.228.37:8000`

---

## MCP Tools (Optional)

The app also exposes 7 tools via MCP at `http://localhost:8000/mcp` for any MCP-compatible client:

| Tool | Description |
|------|-------------|
| `get_recommendations` | Research + generate trade picks. Cached per day. |
| `get_opening_prices` | Fetch actual open prices for a list of symbols. |
| `confirm_trade` | Record trade execution; updates portfolio. |
| `reject_recommendation` | Mark a pending rec as skipped. |
| `get_portfolio` | Live portfolio snapshot with P&L and tax info. |
| `get_market_summary` | EOD index + all S&P sector ETF performance. |
| `read_playbook` | Read the bot's living strategy document. |

To connect an MCP client:

```json
{
  "tradebot": {
    "transport": "http",
    "url": "http://localhost:8000/mcp/"
  }
}
```
