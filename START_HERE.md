# Scorched — Your AI Stock Trading Assistant

## What Is This?

Scorched is a stock trading bot powered by Claude AI that you can actually talk to. Every weekday morning, it:

1. Researches the stock market — price trends, news, analyst ratings, economic data
2. Asks Claude to analyze everything and pick the best trades (if any)
3. A "risk committee" (also Claude) challenges those picks and rejects weak ones
4. Executes the approved trades and tracks your simulated portfolio
5. At the end of the day, reviews how the picks performed and learns from mistakes

You can check in anytime through a **live dashboard**, or open **Claude Desktop and ask your bot questions in plain English** — "what did you buy today?", "how's the portfolio doing?", "what does the playbook say about energy stocks?"

**This starts as paper trading** — simulated money, no real brokerage account needed. You can optionally connect an Alpaca brokerage account later if you want to trade with real money.

The bot runs completely on its own. Once set up, you don't need to do anything — just check the dashboard or chat with your bot when you're curious.

---

## What You'll Need

### The Short Version

1. Install Docker (instructions below)
2. Run: `git clone https://github.com/willcassell/scorched.git && cd scorched`
3. Run: `docker compose up -d --build`
4. Go to `http://localhost:8000/onboarding` and enter your Anthropic API key
5. Done — the bot handles the rest

The rest of this doc covers optional enhancements, data source keys that improve analysis quality, and how to set up the automated daily schedule.

### 1. An Anthropic API Key (required)

This is how you pay for Claude's "brain." The bot makes up to 7 AI calls per day and costs about **$0.15-0.25 per day** (~$5-8/month). Most days it's fewer — the intraday monitor only calls Claude when a position hits a trigger.

- Go to [console.anthropic.com](https://console.anthropic.com)
- Create an account and add a payment method
- Generate an API key (starts with `sk-ant-...`)

### 2. A Computer That Stays On During Market Hours

The bot needs to run from **9:30 AM to 4:05 PM Eastern, Monday through Friday**. It doesn't need to run overnight or on weekends.

Your options, from easiest to most reliable:

| Option | Cost | Best For |
|--------|------|----------|
| **Your home computer** | Free | Trying it out. Just don't let it sleep during market hours. |
| **Oracle Cloud free tier** | Free | Long-term use. A small cloud server that runs 24/7. This is what I use. |
| **Any cloud VM** (DigitalOcean, AWS Lightsail, etc.) | $5-12/mo | If you already have one. |

For the Oracle Cloud free tier (recommended for always-on use):
- Sign up at [cloud.oracle.com](https://cloud.oracle.com) — requires a credit card but the "Always Free" tier is genuinely free
- Create an Ubuntu VM (the ARM "Ampere" shape is free)
- You'll get a public IP address and SSH access

### 3. Docker (the app runs inside it)

**What is Docker?** Think of it as a self-contained box that has everything the bot needs pre-installed — the programming language, the database, all the libraries. You don't need to install Python, PostgreSQL, or anything else manually. Docker handles all of it.

**Installing Docker:**

On **Mac**:
- Download [Docker Desktop](https://www.docker.com/products/docker-desktop/) and install it
- Open it once — it runs in the background

On **Ubuntu/Linux** (your cloud VM):
```bash
# Copy and paste this entire block into your terminal
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# Log out and back in for the group change to take effect
exit
```

On **Windows**:
- Download [Docker Desktop](https://www.docker.com/products/docker-desktop/) and install it
- You may need to enable WSL 2 (Docker will prompt you)

**How to check it's working:**
```bash
docker --version
# You should see something like: Docker version 24.x.x
```

### 4. Git (to download the code)

Git is a tool for downloading and updating code. It's probably already installed.

**Check if you have it:**
```bash
git --version
```

**If not installed:**
- **Mac**: It will prompt you to install Xcode Command Line Tools. Say yes.
- **Ubuntu/Linux**: `sudo apt install git`
- **Windows**: Download from [git-scm.com](https://git-scm.com/download/win)

---

## Setup (15 minutes)

### Step 1: Download the Code

Open a terminal and run:

```bash
git clone https://github.com/willcassell/scorched.git
cd scorched
```

### Step 2: Start the Bot

```bash
docker compose up -d --build
```

This will take 2-3 minutes the first time (it's downloading and building everything). You'll see progress messages. When it's done, you'll get your terminal back.

**Check that it's running:**
```bash
docker compose ps
```

You should see two services listed as "running": `tradebot` and `postgres`.

### Step 3: Open the Setup Wizard

Open your web browser and go to:

```
http://localhost:8000/onboarding
```

(If you're running on a cloud VM, replace `localhost` with your VM's IP address.)

The setup wizard will walk you through:
- Entering your Anthropic API key (required)
- Adding optional data source keys (free — makes the research better, including Twelvedata for broader RSI coverage)
- Choosing your trading strategy (how aggressive, what sectors, hold period, etc.)

### Step 4: See Your Dashboard

After completing the wizard, go to:

```
http://localhost:8000
```

This is your live dashboard. It will be empty at first — the bot generates its first picks at 9:45 AM ET on the next trading day.

### Step 5: Talk to Your Bot (Recommended)

This is the best part. Instead of just watching a dashboard, you can **have a conversation with your trading bot** using Claude Desktop.

**What is this?** Your bot speaks a protocol called MCP (Model Context Protocol) that lets AI assistants like Claude use it as a tool. When you ask Claude a question about your portfolio, Claude calls your bot behind the scenes and answers in plain English. You never need to learn any commands or APIs.

**How to set it up:**

1. Download [Claude Desktop](https://claude.ai/download) if you don't have it
2. Open Claude Desktop → Settings → Developer → Edit Config
3. Add your bot as an MCP server:

```json
{
  "mcpServers": {
    "tradebot": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

(If your bot is running on a cloud VM, replace `localhost` with your VM's IP address.)

4. Restart Claude Desktop

**Now just ask questions in plain English:**

- *"What did you buy today and why?"*
- *"How's my portfolio doing?"*
- *"What does your playbook say — what's working and what isn't?"*
- *"Run today's analysis — what stocks look good?"*
- *"Show me how the market did today"*
- *"Reject recommendation #5 — I don't like that pick"*

Claude handles everything — it figures out which tools to call, gathers the data, and explains it to you like a human analyst would. You don't need to know anything about APIs, trading commands, or technical analysis to use this.

**Cost:** Talking to your bot through Claude Desktop uses your Claude subscription (included with Claude Pro/Team) or API credits. The bot itself costs ~$0.15-0.25/day in Anthropic API usage for its automated daily analysis — your conversations are separate.

### Step 6: Set Up the Daily Schedule (Optional but Recommended)

Even without this step, you can talk to your bot and manually trigger analysis through Claude Desktop. But for a fully hands-off experience, set up scheduled tasks ("cron jobs") so the bot runs its daily cycle automatically.

**On Mac/Linux**, open your terminal and type:
```bash
crontab -e
```

Then paste these lines (they schedule the bot to run at the right times on weekdays):

> **Timezone note:** The cron times below are in **Eastern Time (ET)** — same as NYSE local time. Make sure your VM is configured for `America/New_York` (or use the `setup_cron.py` helper described in the FAQ). No DST math required.

```cron
# Scorched daily trading cycle (times in ET / America/New_York)

# 9:35 AM ET: Post-open data prefetch — live gaps and volume (zero LLM cost)
35 9 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase0.py >> ~/scorched/cron.log 2>&1

# 9:45 AM ET: Claude analysis + recommendations (uses real opening data)
45 9 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase1.py >> ~/scorched/cron.log 2>&1

# 9:55 AM ET: Circuit breaker safety checks (with 25 min of live data)
55 9 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase1_5.py >> ~/scorched/cron.log 2>&1

# 10:15 AM ET: Submit approved orders (post opening-range volatility)
15 10 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase2.py >> ~/scorched/cron.log 2>&1

# 10:45 AM ET: Reconcile pending Alpaca fills + sync positions
45 10 * * 1-5 cd ~/scorched && python3 cron/tradebot_reconcile.py >> ~/scorched/cron.log 2>&1

# 9:35 AM–3:55 PM ET: Intraday position monitoring (every 5 min, self-gates)
*/5 9-15 * * 1-5 cd ~/scorched && python3 cron/intraday_monitor.py >> ~/scorched/cron.log 2>&1

# 4:01 PM ET: End-of-day review and learning
01 16 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase3.py >> ~/scorched/cron.log 2>&1
```

Save and close. The bot will now run automatically every trading day.

**Want Telegram notifications?** Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to your `.env` file and the bot will message you when it makes trades. See [this guide](https://core.telegram.org/bots#how-do-i-create-a-bot) to create a Telegram bot.

---

## What Happens Each Day

| Time (ET) | What the Bot Does |
|-----------|-------------------|
| **9:35 AM** | **Phase 0:** Post-open data prefetch — prices with live opening gaps/volume, news, analyst ratings, insider activity, economic indicators, momentum screener. Zero LLM cost. |
| **9:45 AM** | **Phase 1:** Loads cached data. Asks Claude to analyze ~80 stocks and pick up to 3 trades. Uses real opening data, not stale pre-market prices. |
| **9:55 AM** | **Phase 1.5:** Safety check with 25 min of live data — blocks buys where the stock gapped, drifted from Claude's price, or the market looks dangerous (SPY/VIX). |
| **10:15 AM** | **Phase 2:** Submits approved orders post opening-range (avoids the first 30 min of high volatility). |
| **10:45 AM** | **Phase 2.5:** Reconciles Alpaca fills 30 min after submission, syncs positions against the broker as source of truth. |
| **9:35 AM–3:55 PM** | **Intraday:** Checks held positions every 5 minutes against 5 triggers (position drop, SPY drop, VIX spike, volume surge). If any fire, Claude decides whether to exit. Zero cost on quiet days. |
| **4:01 PM** | **Phase 3:** Reviews the day's performance. Updates its "playbook" — a living document of what strategies are working and what isn't. Tomorrow's picks will reflect today's lessons. |

---

## Useful Commands

```bash
# See what the bot is doing right now
docker compose logs tradebot --tail=50

# Restart the bot after changing settings
docker compose up -d --build tradebot

# Stop everything (your data is preserved)
docker compose down

# Start it back up
docker compose up -d

# Nuclear option — wipe everything and start fresh
docker compose down -v
docker compose up -d --build
```

---

## FAQ

**How much does it cost to run?**
About $5-8/month for the Claude API (up to 7 AI calls per day, though the intraday monitor only calls Claude when triggered — most days it adds zero cost). Everything else is free — all data sources (Alpaca, FRED, Twelvedata, Alpha Vantage, Finnhub) have free tiers, Docker is free, and Oracle Cloud VM is free if you use one.

**Is this real money?**
Not by default. It starts as paper trading with simulated $100,000. You can optionally connect an Alpaca brokerage account later if you want to go live.

**Can I change the trading strategy?**
Yes — go to `http://localhost:8000/strategy` in your browser. You can change everything: how aggressive it trades, what sectors to focus on, hold periods, risk tolerance, and more. Changes take effect the next morning.

**What if I want to stop it?**
Run `docker compose down`. Your portfolio data is saved. Run `docker compose up -d` whenever you want to start again.

**What if something goes wrong?**
Check the logs: `docker compose logs tradebot --tail=100`. The bot logs everything it does, including any errors from external data sources.

**Do I need to know how to code?**
No. The setup wizard handles configuration, the dashboard shows results, and the cron jobs run automatically. You can talk to your bot through Claude Desktop in plain English — ask it anything about your portfolio, its strategy, or the market. The only "technical" part is the initial Docker install and the one-time Claude Desktop config.

**What can I ask my bot?**
Anything about your portfolio, its trades, the market, or its strategy. It pulls live data when you ask. Some examples: "Why did you buy NVDA?", "What's my total return?", "How did the market do today?", "What does your playbook say about holding through earnings?" The bot has 7 tools that Claude calls automatically — you never need to know they exist.

**The dashboard shows no recommendations — what's wrong?**
The bot only generates picks at 9:45 AM ET on weekdays when the market is open. If you just started it, wait until the next trading day morning, or trigger it manually right now with:
```bash
curl -s -X POST http://localhost:8000/api/v1/recommendations/generate \
  -H "Content-Type: application/json" \
  -d '{}'
```
Check the result — if it says `"market_closed": true` that means today is a holiday or weekend and that's expected behavior.

**Do I need to update anything when the clocks change?**
No. The cron jobs run in **Eastern Time (NYSE local time)** directly, so DST changes don't affect the schedule. The Docker container sets `TZ=America/New_York`, and each cron script also runs a sanity check that warns via Telegram if it fires at the wrong ET hour.

If your VM is in UTC and you'd rather not change the system timezone, run `python3 scripts/setup_cron.py` and it will compute the right cron entries for your host.

**How do I set up the cron jobs automatically?**
Instead of manually editing your crontab, run:
```bash
python3 scripts/setup_cron.py
```
Installs every phase entry, computing host-local times if the VM isn't already on Eastern. Use `--check` to verify, `--dry-run` to preview, or `--remove` to uninstall.
