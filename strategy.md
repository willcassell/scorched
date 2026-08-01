# Scorched — Strategy Reference (human-readable)

> **This file is for human reference only. The bot does NOT read it.**
> Source of truth: `strategy.json` (edited via the dashboard at `/strategy`)
> and `analyst_guidance.md` (the framework the LLM is conditioned on).

## Current strategy snapshot

> **⚠️ ACTIVE EXPERIMENT (C-best-in-class, 2026-08-03 → ~2026-10-27):** Opus 5 + all Experiment B carryovers (breakout-only entries, code-enforced factor gate, 15% max size, trailing-stop exits — no fixed profit targets) PLUS the new mechanical entry gate, re-entry cooldown, and exposure discipline. Kill criterion: retire unless window return beats SPY's window return AND window profit factor > 1.0 over 60 trading days. Experiment B was reset here (was passing its own criterion at reset time — see `.handovers/2026-08-01-experiment-C.md` for the why and the evidence trail).

- **Horizon:** 2–6 week holds (swing/position)
- **Entry styles:** Breakout only (mean-reversion suspended for the experiment)
- **Sell discipline:** Trailing stop (let winners run — no fixed profit target)
- **Loss management:** Hybrid (time-based + price-based)
- **Position sizing:** conviction-weighted, up to 15% of portfolio per position (hard cap in `strategy.json`; reduced from 33% for the experiment)
- **Cash floor:** 10% of total portfolio value (hard-enforced in code)
- **Max positions:** 10 simultaneous (hard cap in `strategy.json`)
- **Max sector exposure:** 40% of portfolio (code-enforced — buys that breach this cap are rejected in `recommender.py`)
- **Factor gate:** code-enforced — in a momentum-led regime, buys with negative own 20-day return are rejected (`recommender.py:check_factor_alignment`)
- **Re-entry cooldown:** code-enforced — a BUY is rejected if the same symbol has a SELL in trade history within the last 3 NYSE trading days (`reentry_cooldown_days` in `strategy.json`, `recommender.py:check_reentry_cooldown`); blocks whipsaw churn (sell then re-buy days later)
- **Mechanical entry gate:** code-enforced — every BUY must clear 5-day momentum > 0%, relative volume ≥ 1.0x, and price above the 20-day MA (`mechanical_entry` in `strategy.json`, `recommender.py:check_mechanical_entry`); Claude selects/vetoes among mechanically-qualified names, it does not grant exceptions
- **Stop loss:** -8% from entry (catastrophe backstop)
- **Time stop:** 30 calendar days flat/down with no fresh catalyst
- **Exposure target:** 60–90% invested when SPY is above its 20-day MA and the drawdown gate is clear (advisory, not code-blocking — see below)

## How the bot applies this

Every morning the Phase 1 Claude analysis prompt receives:
1. The current `strategy.json` values, rendered into prose
2. The full `analyst_guidance.md` text as its framework

If the two disagree, Claude gets contradictory instructions and behaviour is unpredictable. Keep them in sync — see `feedback_strategy_doc_sync.md` in memory.

## Forward-looking risk monitors

The hard stops (-8% per position) and the drawdown gate (block buys when portfolio is >8% off peak) are **reactive** — they fire after a loss has already happened. To complement them, the bot exposes a **forward-looking** plausible-loss estimate via portfolio-level Value-at-Risk and Conditional VaR.

`GET /api/v1/portfolio/risk?confidence=0.95&lookback_days=252` runs a historical-simulation VaR/CVaR over the current holdings using market-value weights and Alpaca daily bars. Cash is treated as risk-free. The endpoint returns both percentage and dollar figures (negative = loss). VaR(95) answers "how bad is the 5th-percentile single-day move on this portfolio mix?" and CVaR(95) answers "if we land in that worst 5% tail, what's the average loss?". Treat these as decision aids — they are operator-facing on the dashboard and are also injected into the Phase 1 context so Claude can see when a proposed buy materially expands portfolio tail risk vs. the current holdings.

Per-stock GARCH(1,1) forward-vol forecasts are rendered next to ATR in the same Phase 1 context (regime: `expanding` / `stable` / `contracting`). These are sizing levers, not kill switches — see `analyst_guidance.md` for interpretation.

## Exposure target (advisory)

`strategy.json`'s `exposure` section declares a target invested band —
`target_min_invested_pct: 60`, `target_max_invested_pct: 90` — for when the
regime is healthy (`regime_condition: "spy_above_20dma_and_no_drawdown_gate"`:
SPY above its 20-day moving average AND the drawdown gate clear). Unlike the
sector/position/holdings caps, **the floor is not code-enforced** — code
cannot force a good buy, so falling below 60% invested in a healthy regime
never blocks or forces a trade. Instead, `recommender.py:assess_exposure()`
classifies each session as `underinvested` / `in_range` / `overinvested` /
`defensive_ok` (below floor but the regime isn't healthy — reduced exposure
is appropriate, not a shortfall) and:

1. Injects an `EXPOSURE STATUS` block at the very top of the Phase 1
   context, ahead of any candidate data.
2. When `underinvested`, points Claude at **hard rule #10** in
   `analyst_guidance.md`, which requires either proposing enough qualifying
   buys to move materially toward the floor, or explicitly naming which
   entry criterion failed for the best remaining candidate per vacancy —
   "no compelling setups" with no names is not acceptable.
3. Writes a `gate_decisions` row (`gate="exposure_check"`) every session
   regardless of verdict, so "underinvested while the regime was healthy"
   days are auditable without parsing logs.

The **ceiling** (`overinvested`) is informational only — it's already bound
by the existing position/cash/holdings/sector gates, which stay
code-enforced exactly as before.

## Validating strategy edits with the backtester

Before changing any numeric in `strategy.json` (stop %, target %, RSI band, volume multiplier, sector cap, etc.), run the backtester to compare the proposed value against the current value on the same universe of trades. Two modes are available, both via `scripts/backtest.py` and both producing the same metric set so results can be diffed directly:

- `python scripts/backtest.py replay --stop-pct <new>` — re-runs every actual entry from `TradeHistory` against alternate stop / target / time-stop rules. Best for validating *exit* changes.
- `python scripts/backtest.py sim --symbols AAPL,MSFT,... --vol-mult <new>` — parameterized rule replay over Alpaca daily bars. Best for validating *entry* rule changes (volume multiplier, RSI band, momentum threshold).

Compare expectancy, win rate, profit factor, and max drawdown between current and proposed values. **Don't merge the change if expectancy goes negative or max drawdown deepens materially** at the proposed value.

The backtester has known limitations: no slippage, no commissions, daily-bar resolution only, single-thread compounding (no overlap modeling). It is built for **comparing** rule variants — use it for ranking the proposed value vs the current value, not for projecting absolute portfolio P&L.
