# Scorched Trading System — Advisor & CPA Reference

*Last updated: April 13, 2026*

This document is written for CPAs, financial advisors, and compliance professionals who need to understand what this automated trading system does, how it makes decisions, what safeguards are in place, and what the tax and risk implications are.

---

## 1. What This System Is

Scorched is an automated stock trading system that uses Claude (Anthropic's AI) to analyze market data and generate short-term trade recommendations for U.S. equities. It operates on a daily cycle during NYSE market hours, Monday through Friday.

**It is not:**
- A registered investment advisor or broker-dealer
- A black-box system — all decisions are logged with full reasoning
- An unsupervised system — multiple automated safeguards gate every trade before execution

**Current operating mode:** Paper trading (simulated) with the option to connect to an Alpaca brokerage account for live execution. The system tracks a simulated portfolio starting from a configurable amount (default $100,000).

---

## 2. Investment Strategy

The system follows a **short-term momentum strategy** with these parameters:

| Parameter | Value |
|-----------|-------|
| **Holding period** | 3–10 trading days (target) |
| **Position sizing** | 15–25% of portfolio per position |
| **Maximum positions** | 3–5 simultaneous |
| **Cash reserve floor** | 10–15% of portfolio always held in cash |
| **Universe** | ~80 large-cap U.S. equities (S&P 500 components) plus a daily momentum screener of the full S&P 500 |
| **Strategy type** | Trend-following momentum — buys stocks with positive price momentum, specific catalysts, and technical confirmation |

**Entry criteria:** 3–8% gain over prior 5 trading days, accelerating volume, identifiable catalyst (earnings beat, analyst upgrade, sector rotation), technical alignment (MACD, moving average crossover, relative strength vs. sector).

**Exit criteria:**
- Profit target: consider partial exit at +8%, full exit at +15%
- Stop loss: exit at -5% from entry price
- Time stop: exit if flat/down after 7 trading days
- ATR-based trailing stop (see Section 5)
- Catalyst invalidation: immediate exit

The strategy is **fully configurable** via a web dashboard. All parameters above can be adjusted by the account owner at any time, and changes take effect the next trading day.

---

## 3. How Decisions Are Made

Each trading day, the system executes a multi-phase pipeline. No single AI call makes a trade unilaterally — every buy passes through multiple independent checks.

### Phase 0 — Data Collection (9:35 AM ET, no AI cost)

The system gathers data from 12+ sources after the market opens, so prices reflect actual opening auctions and overnight gaps rather than stale pre-market values:

| Data Category | Sources | Purpose |
|---------------|---------|---------|
| **Price & volume** | Alpaca Data API (IEX bars, snapshots, 1-year daily history) | Technical analysis, momentum screening |
| **News** | Alpaca News API (headlines + summaries) | Catalyst identification |
| **Screener** | Alpaca Screener (most active / movers) | Candidate sourcing beyond the static watchlist |
| **Fundamentals** | Yahoo Finance — PE, market cap, short ratio | Valuation context |
| **Options data** | Yahoo Finance — put/call ratio, implied volatility, implied 30-day move | Sentiment and risk gauge |
| **Earnings & insiders** | Yahoo Finance (earnings dates) + SEC EDGAR (Form 4 filings) | Event risk + corporate signal |
| **Macro indicators** | FRED — fed funds rate, yield curve, CPI, unemployment, PCE, credit spreads, industrial production | Market regime assessment |
| **Economic calendar** | FRED releases API — upcoming CPI, Jobs, FOMC, GDP, PPI, PCE | Volatility risk awareness |
| **Analyst consensus** | Finnhub — buy/hold/sell counts, recommendation trends | Institutional sentiment |
| **Technical indicators** | Computed locally — RSI (Twelvedata for full watchlist, Alpha Vantage fallback for screener picks), MACD, Bollinger Bands, 50/200 MA crossover, support/resistance, volume profile, ATR | Entry/exit timing |
| **Sector analysis** | 11 sector ETF returns via Alpaca bars, per-stock relative strength vs. sector | Rotation identification |

All data is cached locally to `/app/logs/tradebot_research_cache_{date}.json`. Phase 1 does not make any external data calls.

> **April 2026 data source change:** Polygon.io was removed; Alpaca Data API now provides prices, bars, snapshots, news, and the screener — all on the free IEX tier.

### Phase 1 — AI Analysis (9:45 AM ET)

Three sequential AI calls, each with a distinct role:

**Call 1 — Market Analyst (Claude Sonnet 4.6 with extended thinking)**
- Reviews all macro, sector, and per-stock data
- Applies a structured 6-step framework: macro environment → sector rotation → stock screening → ranking → existing position review → output
- Produces a written analysis and a shortlist of 3–8 candidate symbols
- Uses "extended thinking" (16,000 token budget) for deeper reasoning

**Call 2 — Trade Decision (Claude Sonnet 4.6)**
- Receives the analysis, options data for candidates, and current portfolio state
- Makes specific buy/sell recommendations with quantities, confidence levels, reasoning, and key risks
- Follows the user's strategy configuration and the system's "playbook" (a living document of accumulated lessons)

**Call 3 — Risk Committee (Claude Sonnet 4.6)**
- Acts as an **adversarial reviewer** with a default-reject stance
- Reviews every proposed buy against: portfolio concentration, correlation with held positions, market conditions, catalyst strength, and risk/reward
- Can approve or reject each recommendation independently
- **Sells always pass through** — the risk committee cannot block an exit
- Rejected buys are removed from the pipeline before execution

### Phase 1.5 — Circuit Breaker (9:55 AM ET)

A programmatic safety gate (no AI involved) that checks real-time market conditions:

| Check | Threshold | Action |
|-------|-----------|--------|
| Individual stock gap-down from prior close | >2% | Block that buy |
| Stock price drift from AI's suggested price | >1.5% | Block that buy |
| Stock gap-up from prior close | >5% | Block that buy (chase risk) |
| S&P 500 (SPY) gap-down | >1% | Block ALL buys |
| VIX level | >30 | Block ALL buys |
| VIX overnight spike | >20% | Block ALL buys |

All thresholds are configurable. Sells are never blocked.

### Phase 2 — Trade Submission (10:15 AM ET)

Approved recommendations are submitted to the configured broker (paper or Alpaca) **after the opening-range volatility window** (first 30 min of trading sees 2–3× normal volatility). The system:
- Fetches the actual current market price (does not rely on the AI's suggested price)
- Submits limit orders with a small slippage buffer (default ±0.3%)
- Enforces the 10% minimum cash reserve — skips any buy that would violate it
- Checks for wash sale conditions (same symbol sold within prior 30 days)
- For Alpaca, this is **fire-and-forget**: the order is submitted with an idempotency key and recorded as a pending fill; Phase 2.5 reconciles the actual execution
- Records the trade with full audit trail: price, quantity, reasoning, confidence, risks, timestamp

### Phase 2.5 — Reconciliation (10:45 AM ET)

Thirty minutes after submission, the system:
- Polls Alpaca for the status of every pending order from Phase 2
- Records filled orders into the local DB (Portfolio, Position, TradeHistory)
- Runs a position sync that compares local DB positions against Alpaca holdings (Alpaca is the source of truth) and auto-corrects any mismatches
- Sends a Telegram summary with fills, slippage, and any sync corrections

This split avoids blocking Phase 2 with polling timeouts that previously caused "ghost positions" (orders filling on Alpaca without local DB recording).

### Intraday Monitoring (9:35 AM – 3:55 PM ET, every 5 minutes)

Positions are checked against five configurable triggers:

| Trigger | Default Threshold |
|---------|-------------------|
| Position drop from entry price | -5% |
| Position drop from today's open | -3% |
| S&P 500 intraday drop | -2% |
| VIX above absolute level | >30 |
| Volume surge vs. average | >3x |

When a trigger fires, the system calls Claude (Haiku 4.5, the fastest/cheapest model) to evaluate whether to exit. If Claude recommends exit, the sell is executed automatically. **On days where no triggers fire, this costs $0.**

### Phase 3 — End of Day (4:01 PM ET)

- Reviews all open positions with current prices
- Generates hold/tighten/partial/exit recommendations for each position
- Updates the "playbook" — a living strategy document that captures lessons learned
- Sends a summary notification (if Telegram is configured)

### Weekly Reflection (Sunday 6 PM ET)

- Reviews all trades from the past week
- Compares predicted outcomes vs. actual results
- Extracts learnings and strategy adjustments
- Appends findings to the playbook for the following week

---

## 4. Risk Management Safeguards

The system has **seven independent layers of risk management**, any one of which can prevent a trade:

### Layer 1 — Strategy Constraints
- Maximum simultaneous positions (configurable, default 3–5)
- Minimum cash reserve (configurable, default 10–15%)
- Sector concentration limits (configurable, default max 40% in any sector)
- Position size limits (configurable, default 15–25% per position)

### Layer 2 — AI Risk Committee (Call 3)
- Default-reject stance — each buy must be affirmatively approved
- Reviews portfolio correlation, concentration, catalyst quality, and risk/reward
- Operates independently from the analyst and trader AI calls
- Cannot be overridden programmatically

### Layer 3 — Circuit Breaker (Phase 1.5)
- Pure programmatic checks — no AI discretion
- Gates on market-level (SPY, VIX) and stock-level (gap, drift) conditions
- Fires between recommendation and execution
- All thresholds configurable

### Layer 4 — Drawdown Gate
- Monitors portfolio value vs. historical peak (high-water mark)
- **Blocks all new buys** when drawdown exceeds threshold (default 8%)
- Resets only when portfolio recovers
- Sells always permitted

### Layer 5 — Correlation Check
- Before accepting a buy, computes 20-day Pearson return correlation with all held positions
- Correlation > 0.8 triggers a warning injected into the risk review prompt
- Prevents concentration in stocks that move together (e.g., multiple semiconductor names)

### Layer 6 — Trailing Stops
- ATR-based (Average True Range, 14-day) trailing stop on every position
- Stop = high_water_mark - (2 × ATR), with a floor of -5% from entry
- High-water mark ratchets up, stop never moves down
- Checked every 5 minutes during market hours by the intraday monitor

### Layer 7 — Wash Sale Detection
- Before executing a buy, checks if the same symbol was sold at a loss within the prior 30 days
- If detected, a wash sale warning is prepended to the recommendation's risk field
- The trade is not automatically blocked (this is a warning for review, since the wash sale rule is a tax matter rather than a trading risk)

---

## 5. Tax Treatment

### Classification

All realized gains and losses are classified as **short-term** or **long-term** based on holding period:

| Classification | Holding Period | Default Tax Rate |
|----------------|---------------|------------------|
| **Short-term** | < 365 calendar days | 37% (ordinary income) |
| **Long-term** | ≥ 365 calendar days | 20% (capital gains) |

Given the strategy's 3–10 day target holding period, **virtually all realized gains will be short-term** and taxed at ordinary income rates.

### Tax Rates

The system applies configurable tax rates for estimating after-tax returns. Defaults:
- Short-term: 37% (top federal ordinary income bracket)
- Long-term: 20% (top federal LTCG bracket)

These are used for **display/estimation purposes only** (the dashboard shows pre-tax and estimated post-tax P&L). The system does not file taxes or generate tax forms. **State taxes are not included** in the estimates.

### Wash Sale Awareness

The system detects potential wash sales: if a buy order is placed for a symbol that was sold at a loss within the preceding 30 days, the recommendation is flagged with a "WASH SALE WARNING." The system does **not** automatically adjust cost basis for wash sales — that is a tax preparation function.

### What This System Does NOT Do

- Does not generate 1099-B forms (the broker handles this for live trading)
- Does not compute net investment income tax (3.8% NIIT)
- Does not handle state tax calculations
- Does not account for tax-loss harvesting opportunities (could be added)
- Does not track wash sale basis adjustments across positions

### Record Keeping

All trades are recorded in a PostgreSQL database with:
- Symbol, action (buy/sell), quantity, execution price, timestamp
- Cost basis (average cost method), first purchase date
- Realized gain/loss and tax classification on each sell
- Full AI reasoning and confidence level for each recommendation

The trade history is available via the dashboard and API, and can be exported for tax preparation.

---

## 6. Costs

### AI Costs

| Component | Model | Frequency | Estimated Cost |
|-----------|-------|-----------|----------------|
| Analysis + Decision + Risk Review | Claude Sonnet 4.6 | Once daily | ~$0.10–0.15/day |
| EOD Position Review | Claude Haiku 4.5 | Once daily | ~$0.01/day |
| Playbook Update | Claude Sonnet 4.6 | Once daily | ~$0.02–0.05/day |
| Intraday Exit Evaluation | Claude Haiku 4.5 | Only when triggered | ~$0.01/trigger |
| Weekly Reflection | Claude Sonnet 4.6 | Once weekly | ~$0.05/week |

**Total estimated AI cost: $0.15–0.25/day (~$4–8/month)**

On quiet days when no intraday triggers fire, the cost is at the low end. The system tracks token usage and displays a daily cost progress bar on the dashboard.

### Data Source Costs

| Source | Cost |
|--------|------|
| Alpaca Data API | Free (IEX feed; same key as broker) |
| Yahoo Finance (yfinance) | Free |
| FRED (Federal Reserve) | Free (API key required) |
| Twelvedata | Free tier (800 calls/day) |
| Alpha Vantage | Free tier (25 calls/day; RSI fallback) |
| Finnhub | Free tier (analyst consensus) |
| SEC EDGAR | Free |
| Polygon.io | **Removed April 2026** — Alpaca news replaced it |

### Broker Costs

If connected to Alpaca:
- Paper trading: Free
- Live trading: Commission-free equity trades (standard Alpaca terms)
- Alpaca may charge for market data subscriptions depending on plan

### Infrastructure Costs

- Oracle Cloud free-tier VM: $0/month (recommended setup)
- Alternative cloud VMs: $5–12/month
- Running on local machine: $0 (must stay on during market hours)

---

## 7. Performance Tracking & Benchmarks

The system tracks performance against five benchmarks:

| Benchmark | What It Represents |
|-----------|-------------------|
| **SPY** | S&P 500 (market return) |
| **QQQ** | Nasdaq 100 (tech-heavy) |
| **RSP** | S&P 500 Equal Weight (removes mega-cap bias) |
| **MTUM** | iShares MSCI USA Momentum Factor ETF |
| **SPMO** | Invesco S&P 500 Momentum ETF |

The last two are particularly relevant — they represent what a passive momentum strategy would return. If Scorched does not outperform MTUM/SPMO over time, a low-cost momentum ETF would be the better choice.

### Trade-Level Metrics Tracked

- Win rate (% of trades with positive return)
- Profit factor (gross gains / gross losses)
- Average win vs. average loss
- Expectancy (average profit per trade)
- Maximum drawdown (peak-to-trough portfolio decline)
- Average holding period
- Maximum consecutive losses

All metrics are displayed on the dashboard and updated in real-time.

---

## 8. Audit Trail & Transparency

Every action is logged and auditable:

### Database Records
- **recommendation_sessions**: Date, raw research data, AI response text, full analysis
- **trade_recommendations**: Symbol, action, price, quantity, reasoning, confidence, key risks, approval status
- **trade_history**: Execution price, quantity, realized gain/loss, tax classification, trade date
- **positions**: Current holdings with cost basis, first purchase date, trailing stop levels
- **portfolio**: Cash balance, starting capital, peak value, benchmark start prices
- **api_call_log**: Every external API call with service, endpoint, status, response time, errors

### AI Decision Transparency
- Each recommendation includes written reasoning explaining why the trade was suggested
- The risk committee's approve/reject decision includes its own independent reasoning
- The full analysis text (macro → sector → stock-level) is stored for each trading day
- Extended thinking tokens (the AI's internal reasoning process) are logged

### What the Dashboard Shows
- Portfolio value, cash balance, unrealized and realized P&L
- Each open position with entry price, current price, unrealized gain/loss, days held, trailing stop level
- Today's recommendations with approve/reject status and AI reasoning
- Tax summary with short-term and long-term breakdown
- System health: status of every external data source (green/yellow/red)
- Performance vs. all five benchmarks
- Daily Claude AI cost

---

## 9. What Can Go Wrong

### Market Risks
- **Short-term momentum strategies can underperform in choppy, range-bound markets.** The strategy relies on trends continuing; mean-reversion environments will produce losses.
- **Concentrated positions** (15–25% per position, max 5 positions) mean that a single bad trade can significantly impact the portfolio. This is by design for the strategy type, but the risk should be understood.
- **The system trades only U.S. large-cap equities.** It has no exposure to bonds, commodities, international markets, or alternative assets. It is not a diversified portfolio.

### AI Risks
- **The AI can be wrong.** Claude is a language model, not an oracle. Its analysis is based on the data provided and its training. It does not have access to non-public information, real-time order flow, or institutional positioning data beyond what is publicly available.
- **The AI's reasoning can sound convincing even when wrong.** This is an inherent property of large language models. The multi-call pipeline (analyst → trader → risk committee) is designed to catch errors through adversarial review, but it is not infallible.
- **AI model changes** could affect performance. Anthropic periodically updates Claude models. Performance characteristics may shift when models are updated.

### Operational Risks
- **Cron job failure** — if the scheduled automation fails (server restart, network issue), the system stops trading until fixed. Telegram notifications alert the owner, but there is no automated recovery.
- **Data source outages** — if Yahoo Finance, FRED, or other sources are unavailable, the system logs warnings and proceeds with reduced data. Phase 0 failures cause Phase 1 to fall back to inline data fetching.
- **Broker connectivity** — if the Alpaca API is unavailable during execution, orders may fail. Pending fills are tracked in a crash-recovery file and reconciled on restart.

### What the System Cannot Protect Against
- Flash crashes or market halts
- Corporate fraud or sudden delisting
- Overnight gaps exceeding stop-loss levels (the system only monitors during market hours)
- Systemic market events (the drawdown gate and VIX checks provide partial protection)

---

## 10. Regulatory Considerations

- This system is a **personal trading tool**, not a registered investment advisor, broker-dealer, or fund.
- It does not provide investment advice to others, manage other people's money, or solicit investors.
- If used for live trading, all trades execute through a regulated broker (Alpaca Securities LLC, member FINRA/SIPC).
- The system does not engage in: short selling, margin trading, options trading, futures, or any derivative strategies. It buys and sells common stock only.
- All trades are in **whole shares** (no fractional shares).
- The system respects NYSE market hours and holiday calendars (via `pandas_market_calendars`).

### Pattern Day Trader Considerations

If the Alpaca account executes 4+ day trades within 5 business days and the account equity is below $25,000, the account may be flagged as a Pattern Day Trader (PDT). The system's target holding period of 3–10 days means PDT violations are unlikely but not impossible, particularly during volatile periods when the intraday monitor exits positions on the same day they were opened.

---

## 11. Questions an Advisor Should Ask

1. **What is the actual holding period distribution?** Review the trade history to see if positions are being held for the target 3–10 days or if they're being stopped out earlier.

2. **What is the realized short-term gain/loss for the tax year?** All gains from this strategy will be short-term. Estimate the tax liability using the client's actual marginal rate, not the system's default 37%.

3. **Is there wash sale exposure?** The system flags potential wash sales but does not adjust cost basis. Review the trade log for same-symbol round-trips within 30-day windows.

4. **How does this fit in the client's overall portfolio?** This is a concentrated, short-term equity strategy. It should be sized appropriately relative to the client's total assets, risk tolerance, and investment objectives. It is not a replacement for a diversified portfolio.

5. **What is the maximum drawdown tolerance?** The system's drawdown gate defaults to 8% from peak. Discuss whether this is appropriate for the client.

6. **Is the AI cost tax-deductible?** The $4–8/month in Claude API costs, along with any cloud hosting costs, may be deductible as investment expenses, depending on the client's tax situation and whether the investment activity rises to the level of a trade or business. Consult IRS Publication 550.

7. **What happens if the system is offline?** Open positions remain open. There is no automatic liquidation if the system stops running. The client should understand this risk.

---

## 12. Data Access & Export

The following data is available for advisor/CPA review:

| Data | Access Method |
|------|--------------|
| Trade history (all buys/sells with P&L) | Dashboard or `GET /api/v1/portfolio` API |
| Current positions with cost basis | Dashboard or API |
| Tax summary (ST/LT breakdown) | Dashboard |
| Daily AI analysis and reasoning | Dashboard (analysis page) |
| API and system health | Dashboard (`/system` page) |
| Full database | PostgreSQL direct access (on the VM) |

All data is stored in a PostgreSQL database on the VM and persists across restarts. The database can be backed up with standard `pg_dump` commands.
