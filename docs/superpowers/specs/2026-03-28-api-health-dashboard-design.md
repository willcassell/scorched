# API Health Tracking & System Dashboard — Design Spec

## Goal

Add API call success/failure tracking to every external service the bot calls, surface it as a compact status bar on the main dashboard, and provide a dedicated `/system` page with full operational detail.

## Design Decisions (from brainstorming)

- **Status bar style:** Grouped summary in header — "APIs 5/1/1" with green/yellow/red counts + broker badge + SYSTEM link (Option B)
- **System page style:** Operations dashboard with per-API cards, recent error log, 7-day trend heatmap (Option A)
- **Storage:** Database table (`api_call_log`) with 30-day auto-cleanup (Option 2)

## Architecture

### 1. API Call Tracker (Backend)

A lightweight decorator/wrapper that records every external API call to the `api_call_log` DB table.

**Table: `api_call_log`**
| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | Auto-increment |
| `service` | String(20) | "yfinance", "fred", "polygon", "finnhub", "alpha_vantage", "edgar", "claude", "alpaca" |
| `endpoint` | String(100) | E.g. "history", "recommendation_trends", "submit_order" |
| `status` | String(10) | "success", "error", "timeout", "rate_limited" |
| `response_time_ms` | Integer | Milliseconds from request to response |
| `error_message` | Text, nullable | Error details when status != "success" |
| `symbol` | String(10), nullable | Which ticker this call was for (if applicable) |
| `created_at` | DateTime | Server default now() |

**Recording calls:** A context manager `track_api_call(db, service, endpoint, symbol=None)` that wraps each external call. It measures elapsed time, catches exceptions, classifies the result (success/error/timeout/rate_limited), and inserts a row. This is called inside the existing sync fetch functions in `research.py`, `finnhub_data.py`, and `broker/alpaca.py`.

Since the research fetch functions are sync (run in executor), the tracker needs to work synchronously. It accumulates call records in a list, and the async caller bulk-inserts them after the executor completes.

**Cleanup:** A daily cleanup runs during the lifespan startup (or as part of Phase 3 EOD). Deletes rows older than 30 days: `DELETE FROM api_call_log WHERE created_at < now() - interval '30 days'`.

### 2. System Health API Endpoint

**`GET /api/v1/system/health`** — Returns aggregated health data for the dashboard status bar and system page.

Response:
```json
{
  "services": {
    "yfinance": {
      "status": "green",
      "today_success": 247,
      "today_total": 247,
      "today_pct": 100.0,
      "avg_response_ms": 1200,
      "last_error": null,
      "last_error_at": null
    },
    "polygon": {
      "status": "yellow",
      "today_success": 68,
      "today_total": 87,
      "today_pct": 78.2,
      "avg_response_ms": 350,
      "last_error": "429 Too Many Requests",
      "last_error_at": "2026-03-28T08:31:15Z"
    }
  },
  "summary": {
    "green": 5,
    "yellow": 1,
    "red": 1,
    "total": 7
  },
  "broker": {
    "mode": "alpaca_paper",
    "status": "green"
  }
}
```

Status thresholds:
- **Green:** >= 90% success rate today (or no calls today — assume OK)
- **Yellow:** 50-89% success rate today
- **Red:** < 50% success rate today, or last call failed and was within last hour

**`GET /api/v1/system/errors`** — Returns recent errors (last 50).

```json
{
  "errors": [
    {
      "timestamp": "2026-03-28T08:31:02Z",
      "service": "edgar",
      "endpoint": "submissions",
      "status": "timeout",
      "error_message": "ConnectionTimeout after 10s for CIK0000320193",
      "symbol": "AAPL"
    }
  ]
}
```

**`GET /api/v1/system/trend`** — Returns 7-day daily success rates per service.

```json
{
  "days": ["2026-03-20", "2026-03-21", ..., "2026-03-28"],
  "services": {
    "yfinance": [98, 100, 97, 100, 95, 99, 100],
    "polygon": [95, 92, 88, 82, 91, 94, 78]
  }
}
```

### 3. Dashboard Status Bar (Frontend)

Modify the existing header in `dashboard.html` to add the grouped status summary after the LIVE indicator.

Layout:
```
[SCORCHED] [* LIVE]                    [APIs ●5 / ●1 / ●1] [Broker ● ALPACA-PAPER] [SYSTEM]
```

- Fetched from `GET /api/v1/system/health` on page load and every 5-minute refresh
- "SYSTEM" links to `/system`
- If all green, the APIs section is subtle/dim — it only draws attention when yellow/red appear

### 4. System Page (`/system`)

New static HTML file at `src/scorched/static/system.html`, served at `GET /system`.

**Layout (matches existing dark theme):**

**Top section:** Per-API cards in a 4-column grid, 2 rows
- Each card: service name, success % (large number), call count, avg response time
- Left border color matches status (green/yellow/red)
- Cards for: yfinance, FRED, Polygon, Finnhub, Alpha Vantage, EDGAR, Claude API, Alpaca

**Middle section:** Recent Errors
- Scrollable list of recent errors (last 50)
- Each row: timestamp, service badge, error message, symbol
- Color-coded by severity (red for errors, yellow for rate limits)

**Bottom section:** 7-Day Trend
- Grid showing daily success % per service
- Color-coded cells (green >= 90%, yellow 50-89%, red < 50%)
- Shows last 7 trading days

**Navigation:** Back link to dashboard (same pattern as strategy page)

### 5. Instrumenting Existing Code

The API call tracker needs to be wired into every external call site:

| Service | File | Functions to instrument |
|---------|------|----------------------|
| yfinance | `research.py` | `_fetch_price_data_sync`, `_fetch_news_sync`, `_fetch_earnings_surprise_sync`, `_fetch_insider_activity_sync`, `_fetch_options_data_sync`, `_fetch_market_context_sync`, `_fetch_opening_prices_sync`, `_fetch_momentum_screener_sync` |
| FRED | `research.py` | `_fetch_fred_macro_sync` |
| Polygon | `research.py` | `_fetch_polygon_news_sync` |
| Alpha Vantage | `research.py` | `_fetch_av_technicals_sync` |
| EDGAR | `research.py` | `_fetch_edgar_insider_sync` |
| Finnhub | `finnhub_data.py` | `fetch_analyst_consensus_sync` |
| Claude | `recommender.py`, `eod_review.py` | All `claude_call_with_retry` calls |
| Alpaca | `broker/alpaca.py` | `_submit_order_sync`, `_get_order_sync`, `get_all_positions`, `get_account` |

**Approach for sync functions:** Since these run in `run_in_executor`, they can't do async DB writes. Instead, each instrumented function accumulates call records in a thread-local list. After the executor returns, the async caller bulk-inserts the records. A simple pattern:

```python
# In the sync function:
_call_log = []  # module-level, populated by tracker

# After executor returns (async context):
await bulk_insert_call_log(db, _call_log)
_call_log.clear()
```

A cleaner approach: the tracker returns records as a list, and the async gather caller collects and inserts them all at once after the parallel fetch completes.

## What's NOT in Scope

- Real-time WebSocket push (polling every 5 min is fine)
- Alerting (Telegram notification on API failure) — future enhancement
- Historical dashboards beyond 7 days (the data is in DB for 30 days, but the UI shows 7)
- Modifying the strategy settings page
