# Trade History Panel & Reconciliation Alerts

**Date:** 2026-03-31
**Motivation:** Accidental short positions were opened on Alpaca because sell orders were submitted for legacy paper-only positions. The 500 errors went unnoticed because (1) the dashboard has no trade execution visibility, (2) reconciliation is manual-only, and (3) the circuit breaker had a bug preventing it from running.

## Goals

1. Replace the "Recent Closed" dashboard panel with a "Recent Trades" panel showing all buys and sells from the last 30 days
2. Add a reconciliation health indicator to the dashboard header (alongside existing API health dots)
3. Auto-run reconciliation after Phase 2 and intraday sells, with Telegram alerts on mismatch
4. Surface reconciliation mismatches persistently on the dashboard so they can't be missed

## Non-Goals

- Full trade ledger page with filtering/search (future work)
- Automatic mismatch resolution (always requires human investigation)
- Changes to the existing `/broker/status` endpoint response shape

---

## 1. Recent Trades Panel

**Replaces:** "Recent Closed" panel (grid position: column 2, rows 3-6)

**Data source:** Existing `GET /api/v1/portfolio/history?limit=30` — increase limit from 10 to 30. No backend changes needed; the endpoint already returns both buys and sells.

**Display:** Chronological (newest first), all trades from the last 30 days. Each row:

| Column | Content | Width |
|--------|---------|-------|
| Symbol | Ticker | 60px |
| Action | BUY (green badge) / SELL (red badge) | 40px |
| Date | MM/DD format | 55px |
| Shares | Integer quantity | 45px |
| Price | Execution price | 70px |
| P&L | Realized gain/loss for sells; blank for buys | 80px |

**Styling:** Same row style as existing closed-row. BUY badge uses `var(--green)` background, SELL uses `var(--red)` — matching existing action badge pattern.

**Empty state:** "NO TRADES IN LAST 30 DAYS"

**Changes:**
- Rename panel label from "Recent Closed" to "Recent Trades"
- Replace `renderClosed()` function with `renderTrades()` that shows both buys and sells
- Update `loadAll()` to fetch with `limit=30`
- Update `.closed-row` grid template to add shares and price columns

---

## 2. Reconciliation Health Indicator

**Location:** Dashboard topbar, between the API health badge and the SYSTEM link.

**Visual:** A small badge similar to the API health dots:
- `RECON` label + colored dot
- Green dot + "OK" when positions match (or broker mode is `paper`)
- Red dot + "N MISMATCHES" when positions diverge
- Gray dot + "---" while loading or if the check fails

**Behavior:**
- Fetched on every `loadAll()` cycle (every 5 min) by calling `GET /api/v1/broker/status`
- Clicking the badge shows/hides an inline detail panel below the topbar listing each mismatch: symbol, local qty, broker qty
- Detail panel has a red border and dark red background, consistent with error styling
- When broker mode is `paper`, show green "N/A" (reconciliation not applicable)

**Data source:** Existing `GET /api/v1/broker/status` — already returns `reconciliation.has_mismatches` and per-position diffs. No backend changes needed.

---

## 3. Auto-Reconciliation After Trade Execution

### 3a. After Phase 2 (cron/tradebot_phase2.py)

**When:** After all trade confirmations complete, before sending the Telegram summary.

**How:** Call `GET /api/v1/broker/status` from the Phase 2 cron script. If `reconciliation.has_mismatches` is true, append a warning block to the Telegram message:

```
--- RECONCILIATION WARNING ---
Position mismatches detected:
  HAL: local=380, broker=0
  CVX: local=60, broker=-30
Check dashboard for details.
```

**Changes:** Add ~15 lines to `cron/tradebot_phase2.py` after the trade loop, before the Telegram send.

### 3b. After Intraday Sells (src/scorched/api/intraday.py)

**When:** After any sell executes successfully in the `evaluate_triggers` endpoint.

**How:** After processing all triggers, if any sells were executed, call the reconciliation logic inline (reuse the same DB queries from `broker_status.py`). If mismatches found, send a Telegram alert.

**Implementation:** Extract the reconciliation comparison logic from `broker_status.py` into a shared utility function `check_reconciliation(db) -> dict` in a new `src/scorched/services/reconciliation.py` module. Both the `/broker/status` endpoint and the intraday endpoint call this same function.

**Telegram alert format:**
```
TRADEBOT // RECON WARNING (intraday)
Position mismatches after SELL {symbol}:
  {symbol}: local={local_qty}, broker={broker_qty}
```

**Changes:**
- New file: `src/scorched/services/reconciliation.py` — `async def check_reconciliation(db) -> dict` extracts the comparison logic
- Update `src/scorched/api/broker_status.py` to call the shared function
- Update `src/scorched/api/intraday.py` to run reconciliation after sells and send Telegram alert
- Add a `send_telegram()` utility usable from inside the FastAPI app (the cron scripts have their own, but the app process needs one too)

---

## 4. Telegram from FastAPI App

Currently only cron scripts send Telegram messages (via `cron/common.py`). The intraday reconciliation alert needs to send from inside the FastAPI process.

**Implementation:** Add a `src/scorched/services/telegram.py` module with:
```python
async def send_telegram(text: str) -> bool:
    """Send a message via Telegram bot. Returns True on success."""
```

Uses `httpx` (already a dependency) to POST to the Telegram Bot API. Reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from settings. Fails silently with a log warning if tokens aren't configured (dev environments).

---

## Files Changed

| File | Change |
|------|--------|
| `src/scorched/static/dashboard.html` | Replace "Recent Closed" with "Recent Trades" panel; add reconciliation badge to topbar; add mismatch detail panel |
| `src/scorched/services/reconciliation.py` | **New** — shared reconciliation check function |
| `src/scorched/services/telegram.py` | **New** — async Telegram sender for FastAPI process |
| `src/scorched/api/broker_status.py` | Refactor to use shared reconciliation function |
| `src/scorched/api/intraday.py` | Add post-sell reconciliation check + Telegram alert |
| `cron/tradebot_phase2.py` | Add post-trade reconciliation check + Telegram warning |
| `src/scorched/config.py` | Add `telegram_bot_token` and `telegram_chat_id` settings (optional) |

## Testing

- Verify "Recent Trades" panel shows both buys and sells with correct formatting
- Verify reconciliation badge shows green when positions match, red when they don't
- Verify clicking the badge toggles the mismatch detail panel
- Verify Phase 2 cron appends reconciliation warning to Telegram when mismatches exist
- Verify intraday sell triggers reconciliation check and Telegram alert
- Verify paper broker mode shows "N/A" for reconciliation (no Alpaca to compare against)
