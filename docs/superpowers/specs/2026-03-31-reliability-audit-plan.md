# Reliability Audit — Fix Plan

**Date:** 2026-03-31
**Context:** Post-incident audit after accidental Alpaca short positions. Four parallel agents audited broker flow, external APIs, cron scripts, and silent failures.

---

## Critical (Fix Now)

### C1. Alpaca order fills but DB commit fails → state diverges
- **Files:** `src/scorched/api/trades.py`, `src/scorched/broker/alpaca.py`
- **Fix:** Wrap broker calls in try/except in trades.py. If DB commit fails after Alpaca fill, log the Alpaca order ID and send Telegram alert with details for manual reconciliation. Don't silently 500.
- **Status:** [x]

### C2. No idempotency on trade confirmation
- **Files:** `src/scorched/api/trades.py`, `src/scorched/models.py`
- **Fix:** Add unique constraint on `trade_history(recommendation_id)` for non-null values — a recommendation can only produce one trade. Check-then-act race on recommendation status is mitigated because cron is single-threaded, but the constraint is a safety net.
- **Status:** [x]

### C3. asyncio.gather() in recommender has no timeout
- **Files:** `src/scorched/services/recommender.py`
- **Fix:** Wrap the gather in `asyncio.wait_for(..., timeout=300)` (5 min). If timeout fires, log which fetches completed and proceed with partial data rather than hanging forever.
- **Status:** [x]

### C4. Phase 1.5 overwrites Phase 1 file in-place
- **Files:** `cron/tradebot_phase1_5.py`, `cron/tradebot_phase2.py`
- **Fix:** Phase 1.5 writes to a separate file (`/tmp/tradebot_recommendations_gated.json`). Phase 2 reads the gated file if it exists, falls back to the original if not (circuit breaker disabled or crashed). Add atomic writes (write to .tmp, then os.rename).
- **Status:** [x]

### C5. No concurrent execution guard on cron scripts
- **Files:** All cron scripts
- **Fix:** Add PID lock file helper to `cron/common.py`. Each script acquires lock at start, releases on exit. If lock held by a running process, exit immediately.
- **Status:** [x]

---

## High (Fix This Week)

### H1. yfinance calls have no timeout
- **Files:** `src/scorched/services/research.py`, `src/scorched/circuit_breaker.py`, `cron/intraday_monitor.py`
- **Fix:** Set `socket.setdefaulttimeout(30)` at module level in research.py and intraday_monitor.py. This affects all urllib/requests calls made by yfinance within that thread.
- **Status:** [x]

### H2. Cron scripts don't send Telegram on crash
- **Files:** All cron scripts (`cron/tradebot_phase1.py`, `cron/tradebot_phase1_5.py`, `cron/tradebot_phase2.py`, `cron/tradebot_phase3.py`, `cron/intraday_monitor.py`)
- **Fix:** Wrap each script's `main()` call in try/except at `__main__` level. On exception, send Telegram with script name, error type, and truncated message, then re-raise for cron.log.
- **Status:** [x]

### H3. /tmp files not cleaned on failure
- **Files:** `cron/tradebot_phase2.py`
- **Fix:** Move `os.remove(RECS_FILE)` into a `finally` block so it runs whether trades succeed or fail. Add try/except around the remove itself.
- **Status:** [x]

### H4. _get_position_sync swallows all exceptions silently
- **Files:** `src/scorched/broker/alpaca.py`
- **Fix:** Log the exception at WARNING level. Distinguish "position not found" (404 from Alpaca) from "API error" (network/auth). Only fall back to PaperBroker on 404; raise on other errors.
- **Status:** [x]

### H5. Reconciliation runs after trades, not before (Phase 2)
- **Files:** `cron/tradebot_phase2.py`
- **Fix:** Move reconciliation check to before the trade loop. If mismatches detected, include warning in Telegram but still execute (mismatches may be expected legacy positions). Don't block execution — just ensure visibility.
- **Status:** [x]

### H6. Telegram bot token in error logs
- **Files:** `src/scorched/services/telegram.py`
- **Fix:** Don't log the full URL or exception message from httpx (which may contain the URL). Log only "Telegram send failed" with status code if available.
- **Status:** [x]

---

## Medium (Fix Soon)

### M1. Mixed timezone-naive/aware datetimes
- **Files:** `src/scorched/api_tracker.py`, `src/scorched/api/system.py`
- **Fix:** Standardize on naive UTC throughout (matching DB columns). Audit all `datetime.now(timezone.utc)` calls and replace with `datetime.utcnow()`.
- **Status:** [x]

### M2. No retry on Alpaca order submission
- **Files:** `src/scorched/broker/alpaca.py`
- **Fix:** Add simple retry (2 attempts, 3s delay) around `_submit_order_sync`. Only retry on transient errors (network, 5xx), not on 4xx (bad request, insufficient funds).
- **Status:** [x]

### M3. Quantization inconsistency
- **Files:** `src/scorched/services/portfolio.py`, `src/scorched/broker/alpaca.py`
- **Fix:** Document the quantization convention: prices → 0.01, cost basis → 0.0001, shares → 0.000001 (matching DB column definitions). No code change needed if current values match DB schema.
- **Status:** [x]

### M4. Silent partial results from data fetchers
- **Files:** `src/scorched/services/research.py`
- **Fix:** Add a `_data_quality` dict that tracks which sources succeeded/failed per symbol. Log a summary after all fetches complete. Include in Claude prompt: "Note: data unavailable for X sources."
- **Status:** [x]

### M5. Intraday cooldown file race condition
- **Files:** `cron/intraday_monitor.py`
- **Fix:** Use atomic write (write to .tmp, rename) for cooldown file. The PID lock from C5 also prevents concurrent runs.
- **Status:** [x]

### M6. DST requires manual crontab adjustment
- **Files:** crontab (not in repo)
- **Fix:** Document both EST and EDT crontab values in CLAUDE.md. Add a startup time-check in each cron script: if current ET hour doesn't match expected, log a warning.
- **Status:** [x]
