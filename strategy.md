# Scorched — Strategy Reference (human-readable)

> **This file is for human reference only. The bot does NOT read it.**
> Source of truth: `strategy.json` (edited via the dashboard at `/strategy`)
> and `analyst_guidance.md` (the framework the LLM is conditioned on).

## Current strategy snapshot

- **Horizon:** 2–6 week holds (swing/position)
- **Entry styles:** Breakout + Mean reversion
- **Sell discipline:** Scale out
- **Loss management:** Hybrid (time-based + price-based)
- **Position sizing:** conviction-weighted, up to 33% of portfolio per position (hard cap in `strategy.json`)
- **Cash floor:** 10% of total portfolio value (hard-enforced in code)
- **Max positions:** 10 simultaneous (hard cap in `strategy.json`)
- **Max sector exposure:** 40% of portfolio (code-enforced — buys that breach this cap are rejected in `recommender.py`)
- **Stop loss:** -8% from entry
- **Time stop:** 30 calendar days flat/down with no fresh catalyst

## How the bot applies this

Every morning the Phase 1 Claude analysis prompt receives:
1. The current `strategy.json` values, rendered into prose
2. The full `analyst_guidance.md` text as its framework

If the two disagree, Claude gets contradictory instructions and behaviour is unpredictable. Keep them in sync — see `feedback_strategy_doc_sync.md` in memory.

## Forward-looking risk monitors

The hard stops (-8% per position) and the drawdown gate (block buys when portfolio is >8% off peak) are **reactive** — they fire after a loss has already happened. To complement them, the bot exposes a **forward-looking** plausible-loss estimate via portfolio-level Value-at-Risk and Conditional VaR.

`GET /api/v1/portfolio/risk?confidence=0.95&lookback_days=252` runs a historical-simulation VaR/CVaR over the current holdings using market-value weights and Alpaca daily bars. Cash is treated as risk-free. The endpoint returns both percentage and dollar figures (negative = loss). VaR(95) answers "how bad is the 5th-percentile single-day move on this portfolio mix?" and CVaR(95) answers "if we land in that worst 5% tail, what's the average loss?". Treat these as decision aids — they are operator-facing on the dashboard and are also injected into the Phase 1 context so Claude can see when a proposed buy materially expands portfolio tail risk vs. the current holdings.

Per-stock GARCH(1,1) forward-vol forecasts are rendered next to ATR in the same Phase 1 context (regime: `expanding` / `stable` / `contracting`). These are sizing levers, not kill switches — see `analyst_guidance.md` for interpretation.

## Validating strategy edits with the backtester

Before changing any numeric in `strategy.json` (stop %, target %, RSI band, volume multiplier, sector cap, etc.), run the backtester to compare the proposed value against the current value on the same universe of trades. Two modes are available, both via `scripts/backtest.py` and both producing the same metric set so results can be diffed directly:

- `python scripts/backtest.py replay --stop-pct <new>` — re-runs every actual entry from `TradeHistory` against alternate stop / target / time-stop rules. Best for validating *exit* changes.
- `python scripts/backtest.py sim --symbols AAPL,MSFT,... --vol-mult <new>` — parameterized rule replay over Alpaca daily bars. Best for validating *entry* rule changes (volume multiplier, RSI band, momentum threshold).

Compare expectancy, win rate, profit factor, and max drawdown between current and proposed values. **Don't merge the change if expectancy goes negative or max drawdown deepens materially** at the proposed value.

The backtester has known limitations: no slippage, no commissions, daily-bar resolution only, single-thread compounding (no overlap modeling). It is built for **comparing** rule variants — use it for ranking the proposed value vs the current value, not for projecting absolute portfolio P&L.
