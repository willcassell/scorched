# Scorched — Strategy Reference (human-readable)

> **This file is for human reference only. The bot does NOT read it.**
> Source of truth: `strategy.json` (edited via the dashboard at `/strategy`)
> and `analyst_guidance.md` (the framework the LLM is conditioned on).

## Current strategy snapshot

- **Horizon:** 2–6 week holds (swing/position)
- **Entry styles:** Breakout + Mean reversion
- **Sell discipline:** Scale out
- **Loss management:** Hybrid (time-based + price-based)
- **Position sizing:** 15–25% of portfolio per position, conviction-weighted
- **Cash floor:** 10% of total portfolio value (hard-enforced in code)
- **Max positions:** 5 simultaneous
- **Max sector exposure:** 40% of portfolio
- **Stop loss:** -8% from entry
- **Time stop:** 30 calendar days flat/down with no fresh catalyst

## How the bot applies this

Every morning the Phase 1 Claude analysis prompt receives:
1. The current `strategy.json` values, rendered into prose
2. The full `analyst_guidance.md` text as its framework

If the two disagree, Claude gets contradictory instructions and behaviour is unpredictable. Keep them in sync — see `feedback_strategy_doc_sync.md` in memory.
