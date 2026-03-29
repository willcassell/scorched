# Scorched — Your AI Stock Trading Assistant

## What Is This?

Scorched is a stock trading bot powered by Claude AI. Every weekday morning, it:

1. Researches the stock market — price trends, news, analyst ratings, economic data
2. Asks Claude to analyze everything and pick the best trades (if any)
3. A "risk committee" (also Claude) challenges those picks and rejects weak ones
4. Executes the approved trades and tracks your simulated portfolio
5. At the end of the day, reviews how the picks performed and learns from mistakes

You get a live dashboard showing your portfolio, today's picks, trade history, and performance vs. the S&P 500.

**This starts as paper trading** — simulated money, no real brokerage account needed. You can optionally connect an Alpaca brokerage account later if you want to trade with real money.

The bot runs completely on its own. Once set up, you don't need to do anything — just check the dashboard when you're curious.

---

## What You'll Need

### 1. An Anthropic API Key (required)

This is how you pay for Claude's "brain." The bot uses about **$0.10 per day** in API costs (roughly $2-3/month).

- Go to [console.anthropic.com](https://console.anthropic.com)
- Create an account and add a payment method
- Generate an API key (starts with `sk-ant-...`)

### 2. A Computer That Stays On During Market Hours

The bot needs to run from **8:30 AM to 4:05 PM Eastern, Monday through Friday**. It doesn't need to run overnight or on weekends.

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
- Adding optional data source keys (free — makes the research better)
- Choosing your trading strategy (how aggressive, what sectors, hold period, etc.)

### Step 4: See Your Dashboard

After completing the wizard, go to:

```
http://localhost:8000
```

This is your live dashboard. It will be empty at first — the bot generates its first picks at 8:30 AM ET on the next trading day.

### Step 5: Set Up the Daily Schedule (Optional but Recommended)

The bot needs to be told when to wake up and do its work. This is done with "cron jobs" — scheduled tasks that run automatically.

**On Mac/Linux**, open your terminal and type:
```bash
crontab -e
```

Then paste these lines (they schedule the bot to run at the right times on weekdays):

```cron
# Scorched daily trading cycle (times in UTC — adjust for your timezone)
# These times are for US Eastern Daylight Time (Mar-Nov). See DEPLOY.md for winter times.

# 8:30 AM ET: Research and generate recommendations
30 12 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase1.py >> ~/scorched/cron.log 2>&1

# 9:30 AM ET: Circuit breaker safety checks
30 13 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase1_5.py >> ~/scorched/cron.log 2>&1

# 9:35 AM ET: Execute approved trades
35 13 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase2.py >> ~/scorched/cron.log 2>&1

# 4:01 PM ET: End-of-day review and learning
01 20 * * 1-5 cd ~/scorched && python3 cron/tradebot_phase3.py >> ~/scorched/cron.log 2>&1
```

Save and close. The bot will now run automatically every trading day.

**Want Telegram notifications?** Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to your `.env` file and the bot will message you when it makes trades. See [this guide](https://core.telegram.org/bots#how-do-i-create-a-bot) to create a Telegram bot.

---

## What Happens Each Day

| Time (ET) | What the Bot Does |
|-----------|-------------------|
| **8:30 AM** | Researches ~100 stocks: prices, news, analyst ratings, insider activity, economic data. Asks Claude to analyze everything and pick up to 3 trades. |
| **9:30 AM** | Safety check — blocks any buys where the stock gapped down overnight or the market looks dangerous. |
| **9:35 AM** | Executes approved trades at the actual opening price. |
| **4:01 PM** | Reviews the day's performance. Updates its "playbook" — a living document of what strategies are working and what isn't. Tomorrow's picks will reflect today's lessons. |

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
About $2-3/month for the Claude API. Everything else is free (free data sources, free Docker, free Oracle Cloud VM if you use one).

**Is this real money?**
Not by default. It starts as paper trading with simulated $100,000. You can optionally connect an Alpaca brokerage account later if you want to go live.

**Can I change the trading strategy?**
Yes — go to `http://localhost:8000/strategy` in your browser. You can change everything: how aggressive it trades, what sectors to focus on, hold periods, risk tolerance, and more. Changes take effect the next morning.

**What if I want to stop it?**
Run `docker compose down`. Your portfolio data is saved. Run `docker compose up -d` whenever you want to start again.

**What if something goes wrong?**
Check the logs: `docker compose logs tradebot --tail=100`. The bot logs everything it does, including any errors from external data sources.

**Do I need to know how to code?**
No. The setup wizard handles configuration, the dashboard shows results, and the cron jobs run automatically. The only "technical" part is the initial Docker install and the cron setup.
