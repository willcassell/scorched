# Intraday Monitor — Design Spec

**Goal:** A lightweight poller that checks held positions every 5 minutes during market hours. Pure Python trigger checks cost $0.00. Only when a trigger fires does it escalate to a single, focused Claude call that decides whether to exit. If Claude says exit, the sell executes automatically via the broker.

**Date:** 2026-03-29

---

## Architecture

```
cron (every 5 min, 9:35 AM - 3:55 PM ET)
    │
    ▼
intraday_monitor.py
    │
    ├── GET /api/v1/portfolio        → list of held positions
    ├── yfinance batch fetch         → current prices, volumes, SPY, VIX
    ├── Pure trigger checks          → no LLM, no cost
    │
    ├── Nothing triggered?           → exit silently
    │
    └── Trigger fired?
        ├── POST /api/v1/intraday/evaluate  → Claude call (1 per triggered position)
        ├── Claude says "exit"?             → POST /api/v1/trades/confirm (sell)
        └── Telegram notification (always, whether exit or hold)
```

---

## Components

### 1. Trigger Engine — `src/scorched/intraday.py`

Pure functions, no I/O, easy to test. Reuses the `GateResult` dataclass from `circuit_breaker.py`.

Five triggers, all configurable via `strategy.json`:

| Trigger | Config Key | Default | Logic |
|---------|-----------|---------|-------|
| Position drop from entry | `position_drop_from_entry_pct` | 5.0 | `(entry - current) / entry * 100 > threshold` |
| Position drop from today's open | `position_drop_from_open_pct` | 3.0 | `(open - current) / open * 100 > threshold` |
| SPY intraday drop | `spy_intraday_drop_pct` | 2.0 | `(spy_open - spy_current) / spy_open * 100 > threshold` |
| VIX above absolute level | `vix_absolute_max` | 30 | `vix_current > threshold` |
| Volume surge | `volume_surge_multiplier` | 3.0 | `today_volume / avg_20d_volume > threshold` |

Each function signature:

```python
def check_position_drop_from_entry(
    current_price: Decimal,
    entry_price: Decimal,
    threshold_pct: float,
) -> GateResult: ...

def check_position_drop_from_open(
    current_price: Decimal,
    today_open: Decimal,
    threshold_pct: float,
) -> GateResult: ...

def check_spy_intraday_drop(
    spy_current: Decimal,
    spy_open: Decimal,
    threshold_pct: float,
) -> GateResult: ...

def check_vix_level(
    vix_current: Decimal,
    threshold: float,
) -> GateResult: ...

def check_volume_surge(
    current_volume: float,
    avg_volume_20d: float,
    threshold_multiplier: float,
) -> GateResult: ...
```

A top-level `check_intraday_triggers()` function runs all five checks for a single position and returns a list of fired `GateResult`s (may be empty).

A `check_market_triggers()` function runs SPY and VIX checks (shared across all positions).

### 2. API Endpoint — `src/scorched/api/intraday.py`

New router mounted at `/api/v1/intraday`.

**`POST /api/v1/intraday/evaluate`**

Request body:
```json
{
  "triggers": [
    {
      "symbol": "AAPL",
      "trigger_reasons": ["Position dropped 5.8% from entry (threshold: 5.0%)"],
      "current_price": 174.50,
      "entry_price": 185.20,
      "today_open": 183.00,
      "today_high": 183.50,
      "today_low": 174.10,
      "days_held": 4,
      "shares": 50,
      "original_reasoning": "Strong momentum play on AI chip demand..."
    }
  ],
  "market_context": {
    "spy_change_pct": -0.8,
    "vix_current": 22.5
  }
}
```

Response:
```json
{
  "decisions": [
    {
      "symbol": "AAPL",
      "action": "exit_full",
      "reasoning": "Position has broken through stop-loss level...",
      "trade_result": {
        "trade_id": 42,
        "shares": 50,
        "execution_price": 174.50,
        "realized_gain": -535.00
      }
    }
  ]
}
```

Logic:
1. For each triggered position, call `claude_client.call_intraday_exit()` with focused context
2. If Claude says `exit_full` or `exit_partial`, submit sell via `get_broker(db).submit_sell()`
3. Return all decisions (hold or exit) with reasoning

### 3. Claude Prompt — `src/scorched/prompts/intraday_exit.md`

Tight prompt focused on exit decisions only. No market analysis, no new picks.

```
You are reviewing a position that has triggered an intraday alert. Your job is to decide whether to exit immediately, exit partially, or hold.

Consider:
- How severe is the trigger? A -5.1% drop (barely over threshold) is different from -8%.
- Is this stock-specific or a broad market move? If SPY is down similarly, it may be systemic, not thesis-breaking.
- How strong was the original thesis? Is the catalyst still valid despite the price drop?
- How many days held? A day-1 drop may mean bad entry timing; a day-7 drop after gains may mean the trade is done.

Be decisive. If the thesis is broken or the loss is accelerating, exit. If this is normal volatility within the thesis timeframe, hold. Don't hedge — pick one.

Respond with valid JSON only:
{
  "action": "exit_full" or "exit_partial" or "hold",
  "partial_pct": null or 50 (percentage to sell, only if exit_partial),
  "reasoning": "1-2 sentences explaining the decision"
}
```

### 4. Claude Client Addition — `src/scorched/services/claude_client.py`

New function:

```python
def call_intraday_exit(user_content: str):
    """Intraday exit evaluation. Small focused call.
    Returns (response, raw_text).
    """
```

Uses Sonnet, max_tokens=512 (small response), no extended thinking.

### 5. Cron Script — `cron/intraday_monitor.py`

Runs every 5 minutes during market hours. Uses `cron/common.py`.

Flow:
1. Check time — if outside 9:35 AM - 3:55 PM ET, exit immediately
2. `GET /api/v1/portfolio` — get held positions. If none, exit.
3. Batch fetch via yfinance: current prices, today's open/high/low, volume for all held symbols + SPY + VIX. Also fetch 20-day average volume.
4. Load intraday_monitor config from `strategy.json`
5. If `enabled` is false, exit.
6. Check cooldowns — skip any symbol triggered within last 30 minutes
7. Run `check_market_triggers()` for SPY/VIX
8. For each position, run `check_intraday_triggers()` (includes per-position checks + market trigger results)
9. If any triggers fired: `POST /api/v1/intraday/evaluate` with triggered positions
10. Send Telegram for every decision (hold or exit)
11. Update cooldown file

Cron entry:
```cron
# Intraday monitor (every 5 min, 9:35 AM - 3:55 PM ET)
*/5 13-19 * * 1-5 cd ~/tradebot && python3 cron/intraday_monitor.py >> ~/tradebot/cron.log 2>&1
```

Note: The script self-gates on ET time, so the broad UTC cron window (13:00-19:59 UTC) is fine — the script exits immediately if it's outside 9:35 AM - 3:55 PM ET.

### 6. Strategy Config — `strategy.json`

New top-level key:

```json
{
  "intraday_monitor": {
    "enabled": true,
    "position_drop_from_entry_pct": 5.0,
    "position_drop_from_open_pct": 3.0,
    "spy_intraday_drop_pct": 2.0,
    "vix_absolute_max": 30,
    "volume_surge_multiplier": 3.0,
    "cooldown_minutes": 30
  }
}
```

---

## Data Flow

### Typical Day (No Triggers)

```
9:35  → fetch 5 positions + SPY + VIX → all checks pass → exit (0.5s, $0.00)
9:40  → fetch 5 positions + SPY + VIX → all checks pass → exit
...78 iterations...
3:55  → fetch 5 positions + SPY + VIX → all checks pass → exit
```

Total cost: $0.00. Total compute: ~40 seconds across the day.

### Bad Day (1 Trigger at 11:15 AM)

```
11:15 → fetch positions → AAPL down 5.3% from entry → TRIGGER
      → POST /api/v1/intraday/evaluate (1 Claude call, ~$0.01)
      → Claude: "exit_full — thesis broken, stop-loss level breached"
      → broker.submit_sell(AAPL, 50 shares, $174.50)
      → Telegram: "INTRADAY EXIT: Sold 50sh AAPL @ $174.50 (-5.3%). Claude: thesis broken."
11:45 → AAPL on cooldown, skip
```

Total cost: ~$0.01. One sell executed.

---

## Safeguards

- **Market hours only** — script checks ET time before doing anything
- **Cooldown** — 30-minute cooldown per symbol after trigger (configurable). Prevents rapid-fire during slow bleeds. Stored in `/tmp/intraday_cooldown.json`.
- **Sells only** — the monitor never buys, never opens new positions
- **Telegram always** — you get notified whether Claude says hold or exit
- **Enabled flag** — can be disabled in strategy.json without removing the cron job
- **No positions = no work** — if portfolio is empty, script exits in <1 second

---

## Files to Create

| File | Purpose |
|------|---------|
| `src/scorched/intraday.py` | Pure trigger check functions |
| `src/scorched/api/intraday.py` | API endpoint for evaluate |
| `src/scorched/prompts/intraday_exit.md` | Claude prompt for exit decisions |
| `cron/intraday_monitor.py` | Cron poller script |
| `tests/test_intraday.py` | Tests for trigger functions |

## Files to Modify

| File | Change |
|------|--------|
| `src/scorched/services/claude_client.py` | Add `call_intraday_exit()` |
| `src/scorched/main.py` | Mount intraday router |
| `strategy.json` | Add `intraday_monitor` config block |

---

## Cost Summary

| Scenario | Daily Cost |
|----------|-----------|
| Typical day (no triggers) | $0.00 |
| 1 trigger fires | ~$0.01-0.02 |
| 3 triggers fire | ~$0.03-0.06 |
| Worst case (every position triggers) | ~$0.05-0.10 |
