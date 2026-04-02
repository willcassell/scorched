# Next Session Plan — Tradebot Optimization Round 2

Generated: 2026-04-02

## Priority Order

### Block 1: Quick Wins (30 min)
1. **Fix playbook truncation bug** — `recommender.py:423` passes `playbook.content[:500]`, defeating the 1500-char expansion in `risk_review.py`. One-line fix: remove `[:500]`.
2. **Replace `asyncio.get_event_loop()`** — 23 occurrences across 6 files. Pure find-replace with `asyncio.get_running_loop()`.
3. **Replace `datetime.utcnow()`** — 13 occurrences across 7 files. Replace with `datetime.now(timezone.utc)`, add `timezone` imports.
4. **Remove global `socket.setdefaulttimeout(30)`** — 2 occurrences (research.py, circuit_breaker.py). All HTTP calls already have explicit timeouts.

### Block 2: Risk Management (1-2 hrs)
5. **Drawdown limit enforcement** — New `drawdown_gate.py` module + `peak_portfolio_value` column on Portfolio. Check before Claude calls in `generate_recommendations()`. Configurable threshold in `strategy.json` (default 8%). Telegram alert on activation.
6. **Correlation check before buys** — New `correlation.py` module. 20-day price correlation between candidates and held positions. Warnings injected into `key_risks` and risk review prompt (Call 3).

### Block 3: Broker Reliability (1-2 hrs)
7. **Deduplicate AlpacaBroker recording** — Replace `_record_buy`/`_record_sell` inline logic with delegation to `portfolio.apply_buy()`/`apply_sell()`. Must do BEFORE crash recovery.
8. **Crash recovery for post-fill DB recording** — New `pending_fills.py` module. JSON file at `/app/logs/pending_fills.json`. Write before DB recording, delete after. Startup reconciliation in `main.py` lifespan.

### Block 4: LLM Quality (1 hr)
9. **Structured analytical steps in Call 1** — Replace `analysis.md` with 6-step framework: Macro Assessment, Sector Scan, Individual Screening, Candidate Ranking, Position Review, Output.
10. **Pydantic validation of Claude outputs** — `AnalysisOutput`, `DecisionOutput`, `RiskReviewOutput` models in `claude_client.py`. Graceful degradation on validation failure.
11. **Few-shot examples in Call 2** — Add 2 examples to `decision.md`: one trade day, one no-trade day. Double-brace escape for `.format()`.

### Block 5: External API Resilience (30 min)
12. **HTTP retry wrapper** — New `http_retry.py` with `retry_get()` and `retry_call()`. Apply to FRED, Polygon, Alpha Vantage, EDGAR, Finnhub calls. 3 attempts with 1s/3s/5s backoff, only on transient errors.

## Key Implementation Notes

- **Block 2 needs a DB migration** (0007) for `peak_portfolio_value` column
- **Block 3 must be done in order**: dedup first, then crash recovery on top
- **Block 4 prompt changes** should go last — Pydantic validation catches regressions
- **Double-brace escaping** is critical in `decision.md` examples (uses `.format()`)
- **Correlation uses daily returns**, not price levels (statistically correct)
- **Drawdown gate**: Strategy B (filter buys at end) is simplest first pass; Strategy A (skip LLM calls) is a cost optimization for later

## Files Changed per Block

| Block | New Files | Modified Files |
|-------|-----------|---------------|
| 1 | — | recommender.py, research.py, circuit_breaker.py, portfolio.py, alpaca.py, prefetch.py, system.py, api_tracker.py, mcp_tools.py, paper.py |
| 2 | drawdown_gate.py, correlation.py, tests/test_drawdown_gate.py, tests/test_correlation.py, migration 0007 | recommender.py, risk_review.py, models.py, strategy.py, strategy.json, schemas.py |
| 3 | broker/pending_fills.py | broker/alpaca.py, main.py |
| 4 | — | claude_client.py, risk_review.py, prompts/analysis.md, prompts/decision.md |
| 5 | http_retry.py | research.py, finnhub_data.py |
