# Phase 0 — Data Prefetch

**Date:** 2026-04-01
**Motivation:** Phase 1 data fetches take 12-31 minutes for ~120 symbols, causing the 5-min gather timeout to fire and fail the pipeline. The momentum screener alone scans 460 SP500 symbols (7-23 min). Moving data fetching to a separate Phase 0 at 7:30 AM ET gives 60 minutes of headroom and lets Phase 1 focus purely on Claude calls (~3 min).

## Schedule

| Phase | Time (ET) | Duration | What it does |
|-------|-----------|----------|--------------|
| **Phase 0** | 7:30 AM | 10-30 min | Fetch all external data, cache processed text |
| Phase 1 | 8:30 AM | ~3 min | Load cache, run 3 Claude calls |
| Phase 1.5 | 9:30 AM | ~10s | Circuit breaker gate |
| Phase 2 | 9:35 AM | ~1 min | Execute trades |

## What Phase 0 Does

1. Run momentum screener (scan 460 SP500 → top 20)
2. Build research symbol list: WATCHLIST (58) + current positions (~5) + screener (20) = ~80 symbols
3. Parallel fetch for ~80 symbols: price data, news, earnings, insider/EDGAR, market context, FRED macro, Polygon news, Alpha Vantage RSI
4. Finnhub analyst consensus for ~80 symbols (sequential, 1.1s/symbol)
5. Compute technicals from price history
6. Build processed text strings (research context, analyst context)
7. Write cache file (atomic)

Each step is timed and logged with `logger.info("Phase 0: {step} completed in {elapsed:.1f}s")`.

## What Phase 1 Becomes

1. Check for `/tmp/tradebot_research_cache_{date}.json`
2. If found and date matches: load cached data, skip to Claude calls
3. If not found or stale: fall back to inline fetch (existing behavior)
4. Claude calls unchanged — same prompts, same output format

## Screener Change

`n=60` → `n=20` in the recommender call. Reduces research universe from ~120 to ~80 symbols. The screener's internal scan of 460 symbols is unchanged (needed to find the top 20), but it now runs in Phase 0 instead of blocking Phase 1.

## Cache File

Path: `/tmp/tradebot_research_cache_{date}.json` (date-stamped to prevent stale reuse)

```json
{
  "date": "2026-04-01",
  "created_at": "2026-04-01T11:32:00Z",
  "timing": {
    "screener_s": 480.2,
    "price_data_s": 150.3,
    "news_s": 90.1,
    "earnings_s": 85.4,
    "insider_s": 18.2,
    "market_context_s": 22.0,
    "fred_macro_s": 12.5,
    "polygon_news_s": 62.0,
    "av_technicals_s": 24.0,
    "finnhub_s": 88.0,
    "technicals_s": 0.3,
    "build_context_s": 0.1,
    "total_s": 510.5
  },
  "research_symbols": ["AAPL", "MSFT", ...],
  "screener_symbols": ["FANG", "DVN", ...],
  "current_positions": ["SLB", "MRK", "HAL"],
  "research_context": "=== WATCHLIST DATA ===\n...",
  "analyst_context": "## Analyst Consensus\n...",
  "technicals_context": "## Technical Indicators\n...",
  "price_data_summary": {"AAPL": {"current_price": 185.2, "change_pct": 1.2}, ...}
}
```

The text strings are the exact strings that get injected into Claude prompts. `price_data_summary` provides the per-symbol price/change data that `generate_recommendations` uses for building the options fetch list after Call 1.

## New Files

### `src/scorched/api/prefetch.py`
- `POST /api/v1/research/prefetch` endpoint
- Calls the data-fetching functions from research.py (existing code, no duplication)
- Times each step, builds cache dict, writes atomic JSON
- Returns timing summary in response

### `cron/tradebot_phase0.py`
- Cron script: calls `POST /api/v1/research/prefetch`
- Sends Telegram with timing summary on success
- Sends Telegram alert on failure
- Uses PID lock and crash alert (existing patterns from C5/H2)

## Modified Files

### `src/scorched/services/recommender.py`
- At start of `generate_recommendations()`: check for cache file, load if valid
- If cache hit: skip screener, skip gather, skip Finnhub — use cached text strings
- If cache miss: fall back to existing inline fetch (with n=20 screener)
- Change `n=60` to `n=20` in the inline fallback path

### `crontab`
- Add: `30 7 * * 1-5` → Phase 0 (7:30 AM ET during EDT)

## Performance Logging

All timing logged at INFO level with consistent prefix for grep:
```
Phase 0: momentum_screener completed in 480.2s (460 symbols scanned, 20 returned)
Phase 0: price_data completed in 150.3s (82 symbols)
Phase 0: news completed in 90.1s (82 symbols)
Phase 0: finnhub completed in 88.0s (82 symbols)
Phase 0: TOTAL completed in 510.5s
```

Queryable with: `grep "Phase 0:" logs/cron.log` or `docker compose logs tradebot | grep "Phase 0:"`

Timing is also stored in the cache JSON file for historical analysis.

## Error Handling

- Phase 0 crash → Telegram alert, Phase 1 falls back to inline fetch
- Cache file wrong date → Phase 1 ignores, fetches inline
- Cache file corrupt JSON → Phase 1 catches error, fetches inline
- Partial fetch failure (e.g., Polygon rate-limited) → Cache still written with available data, missing sources noted in research context (M4 data quality feature)
- Phase 0 takes >55 min → log warning (cutting into Phase 1 window)
