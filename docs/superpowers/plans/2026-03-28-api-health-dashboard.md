# API Health Tracking & System Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track every external API call's success/failure in a DB table, add a compact health summary to the dashboard header, and build a dedicated `/system` operations page with per-API cards, error log, and 7-day trend.

**Architecture:** New `ApiCallLog` model + Alembic migration. A sync `ApiCallTracker` class collects call records during data fetches (which run in thread executors), then the async caller bulk-inserts them after the executor returns. Three new API endpoints serve aggregated health data. Dashboard header gets a grouped status bar. New `/system` HTML page with full operational detail.

**Tech Stack:** SQLAlchemy (existing), Alembic (existing), FastAPI (existing), vanilla HTML/CSS/JS (matching existing dashboard patterns)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/scorched/models.py` | Add `ApiCallLog` model |
| `src/scorched/api_tracker.py` | `ApiCallTracker` class — sync call recording + async bulk insert |
| `src/scorched/api/system.py` | REST endpoints: `/system/health`, `/system/errors`, `/system/trend` |
| `src/scorched/services/research.py` | Instrument all sync fetch functions with tracker |
| `src/scorched/services/finnhub_data.py` | Instrument Finnhub calls with tracker |
| `src/scorched/services/recommender.py` | Instrument Claude calls, bulk-insert tracker records |
| `src/scorched/broker/alpaca.py` | Instrument Alpaca SDK calls with tracker |
| `src/scorched/main.py` | Register system router, add `/system` page route, add cleanup to lifespan |
| `src/scorched/static/dashboard.html` | Add grouped status bar to topbar |
| `src/scorched/static/system.html` | New operations dashboard page |
| `alembic/versions/0005_api_call_log.py` | Migration for api_call_log table |
| `tests/test_api_tracker.py` | Tests for tracker and health aggregation |

---

## Task 1: ApiCallLog Model + Migration

**Files:**
- Modify: `src/scorched/models.py`
- Create: `alembic/versions/0005_api_call_log.py` (via autogenerate)

- [ ] **Step 1: Add ApiCallLog model to models.py**

After the `TokenUsage` class in `src/scorched/models.py`, add:

```python
class ApiCallLog(Base):
    __tablename__ = "api_call_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    service: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(15), nullable=False)
    response_time_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
```

- [ ] **Step 2: Generate Alembic migration**

Run: `cd /home/ubuntu/tradebot && docker compose exec tradebot alembic revision --autogenerate -m "add api_call_log table"`

If running outside Docker: `cd /home/ubuntu/tradebot && DATABASE_URL=postgresql+asyncpg://scorched:scorched@localhost:5432/scorched alembic revision --autogenerate -m "add api_call_log table"`

- [ ] **Step 3: Apply migration**

Run: `docker compose exec tradebot alembic upgrade head`

- [ ] **Step 4: Commit**

```bash
git add src/scorched/models.py alembic/versions/
git commit -m "feat: add ApiCallLog model and migration"
```

---

## Task 2: ApiCallTracker — Recording and Querying

**Files:**
- Create: `src/scorched/api_tracker.py`
- Create: `tests/test_api_tracker.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_api_tracker.py`:

```python
"""Tests for API call tracker."""
import pytest
import time
from decimal import Decimal
from scorched.api_tracker import ApiCallTracker, track_call, compute_service_health


class TestTrackCall:
    def test_records_success(self):
        tracker = ApiCallTracker()
        with track_call(tracker, "yfinance", "history", symbol="AAPL"):
            time.sleep(0.01)  # simulate work
        assert len(tracker.records) == 1
        rec = tracker.records[0]
        assert rec["service"] == "yfinance"
        assert rec["endpoint"] == "history"
        assert rec["status"] == "success"
        assert rec["symbol"] == "AAPL"
        assert rec["response_time_ms"] >= 10

    def test_records_error(self):
        tracker = ApiCallTracker()
        try:
            with track_call(tracker, "polygon", "news", symbol="NVDA"):
                raise ConnectionError("Connection refused")
        except ConnectionError:
            pass
        assert len(tracker.records) == 1
        rec = tracker.records[0]
        assert rec["status"] == "error"
        assert "Connection refused" in rec["error_message"]

    def test_records_timeout(self):
        tracker = ApiCallTracker()
        try:
            with track_call(tracker, "edgar", "submissions", symbol="AAPL"):
                raise TimeoutError("Request timed out")
        except TimeoutError:
            pass
        rec = tracker.records[0]
        assert rec["status"] == "timeout"

    def test_records_rate_limit(self):
        tracker = ApiCallTracker()
        try:
            with track_call(tracker, "polygon", "news"):
                from urllib.error import HTTPError
                raise HTTPError(None, 429, "Too Many Requests", {}, None)
        except Exception:
            pass
        rec = tracker.records[0]
        assert rec["status"] == "rate_limited"

    def test_multiple_calls(self):
        tracker = ApiCallTracker()
        with track_call(tracker, "fred", "series"):
            pass
        with track_call(tracker, "fred", "series"):
            pass
        assert len(tracker.records) == 2


class TestComputeServiceHealth:
    def test_all_success(self):
        records = [
            {"service": "yfinance", "status": "success", "response_time_ms": 100, "error_message": None, "created_at": "2026-03-28T12:00:00"},
            {"service": "yfinance", "status": "success", "response_time_ms": 150, "error_message": None, "created_at": "2026-03-28T12:01:00"},
        ]
        health = compute_service_health(records)
        assert health["yfinance"]["status"] == "green"
        assert health["yfinance"]["today_pct"] == 100.0

    def test_degraded_service(self):
        records = [
            {"service": "polygon", "status": "success", "response_time_ms": 100, "error_message": None, "created_at": "2026-03-28T12:00:00"},
            {"service": "polygon", "status": "rate_limited", "response_time_ms": 50, "error_message": "429", "created_at": "2026-03-28T12:01:00"},
            {"service": "polygon", "status": "rate_limited", "response_time_ms": 50, "error_message": "429", "created_at": "2026-03-28T12:02:00"},
        ]
        health = compute_service_health(records)
        assert health["polygon"]["status"] == "yellow"

    def test_down_service(self):
        records = [
            {"service": "edgar", "status": "error", "response_time_ms": 10000, "error_message": "timeout", "created_at": "2026-03-28T12:00:00"},
        ]
        health = compute_service_health(records)
        assert health["edgar"]["status"] == "red"

    def test_empty_records(self):
        health = compute_service_health([])
        assert health == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/test_api_tracker.py -v 2>&1 | tail -5`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement ApiCallTracker**

Create `src/scorched/api_tracker.py`:

```python
"""API call tracker — records external API call success/failure for health monitoring.

Usage in sync fetch functions (which run in thread executors):

    tracker = ApiCallTracker()
    for symbol in symbols:
        with track_call(tracker, "yfinance", "history", symbol=symbol):
            data = yf.Ticker(symbol).history(...)
    # After executor returns, caller does:
    await tracker.flush(db)
"""
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import select, func, delete, and_
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Services tracked
SERVICES = ["yfinance", "fred", "polygon", "finnhub", "alpha_vantage", "edgar", "claude", "alpaca"]


class ApiCallTracker:
    """Collects API call records in a thread-safe list.

    Sync code appends records via track_call(). After the executor returns,
    the async caller calls flush(db) to bulk-insert into api_call_log.
    """

    def __init__(self):
        self.records: list[dict] = []

    async def flush(self, db: AsyncSession):
        """Bulk-insert collected records into the database."""
        if not self.records:
            return
        from .models import ApiCallLog
        for rec in self.records:
            db.add(ApiCallLog(
                service=rec["service"],
                endpoint=rec["endpoint"],
                status=rec["status"],
                response_time_ms=rec["response_time_ms"],
                error_message=rec.get("error_message"),
                symbol=rec.get("symbol"),
            ))
        await db.commit()
        count = len(self.records)
        self.records.clear()
        logger.debug("Flushed %d API call records", count)


@contextmanager
def track_call(tracker: ApiCallTracker, service: str, endpoint: str, symbol: str | None = None):
    """Context manager that records an API call's outcome.

    Catches and classifies exceptions, then re-raises them.
    The caller's existing error handling is not affected.
    """
    start = time.monotonic()
    record = {
        "service": service,
        "endpoint": endpoint,
        "symbol": symbol,
        "status": "success",
        "response_time_ms": 0,
        "error_message": None,
    }
    try:
        yield
    except TimeoutError as e:
        record["status"] = "timeout"
        record["error_message"] = str(e)[:500]
        raise
    except Exception as e:
        # Classify rate limits (HTTP 429)
        err_str = str(e)
        if "429" in err_str or "Too Many Requests" in err_str or "rate" in err_str.lower():
            record["status"] = "rate_limited"
        else:
            record["status"] = "error"
        record["error_message"] = err_str[:500]
        raise
    finally:
        record["response_time_ms"] = int((time.monotonic() - start) * 1000)
        tracker.records.append(record)


def compute_service_health(records: list[dict]) -> dict:
    """Aggregate raw call records into per-service health status.

    Returns {service: {status, today_success, today_total, today_pct, avg_response_ms, last_error, last_error_at}}
    """
    from collections import defaultdict
    services = defaultdict(lambda: {"successes": 0, "total": 0, "response_times": [], "last_error": None, "last_error_at": None})

    for rec in records:
        svc = rec["service"]
        s = services[svc]
        s["total"] += 1
        if rec["status"] == "success":
            s["successes"] += 1
        else:
            s["last_error"] = rec.get("error_message", rec["status"])
            s["last_error_at"] = rec.get("created_at")
        s["response_times"].append(rec.get("response_time_ms", 0))

    result = {}
    for svc, s in services.items():
        pct = round(s["successes"] / s["total"] * 100, 1) if s["total"] > 0 else 100.0
        avg_ms = round(sum(s["response_times"]) / len(s["response_times"])) if s["response_times"] else 0

        if pct >= 90:
            status = "green"
        elif pct >= 50:
            status = "yellow"
        else:
            status = "red"

        result[svc] = {
            "status": status,
            "today_success": s["successes"],
            "today_total": s["total"],
            "today_pct": pct,
            "avg_response_ms": avg_ms,
            "last_error": s["last_error"],
            "last_error_at": s["last_error_at"],
        }

    return result


async def cleanup_old_records(db: AsyncSession, days: int = 30):
    """Delete api_call_log records older than `days` days."""
    from .models import ApiCallLog
    cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
    from datetime import timedelta
    cutoff = cutoff - timedelta(days=days)
    result = await db.execute(
        delete(ApiCallLog).where(ApiCallLog.created_at < cutoff)
    )
    await db.commit()
    if result.rowcount:
        logger.info("Cleaned up %d old API call log records", result.rowcount)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/test_api_tracker.py -v 2>&1 | tail -15`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/scorched/api_tracker.py tests/test_api_tracker.py
git commit -m "feat: add ApiCallTracker with sync recording and health aggregation"
```

---

## Task 3: System Health API Endpoints

**Files:**
- Create: `src/scorched/api/system.py`
- Modify: `src/scorched/main.py`

- [ ] **Step 1: Create system health endpoints**

Create `src/scorched/api/system.py`:

```python
"""System health and API monitoring endpoints."""
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select, func, and_, case, cast, Date
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_db
from ..models import ApiCallLog
from ..api_tracker import compute_service_health, SERVICES

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health")
async def system_health(db: AsyncSession = Depends(get_db)):
    """Aggregated health status for all external services."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    rows = (await db.execute(
        select(
            ApiCallLog.service,
            ApiCallLog.status,
            ApiCallLog.response_time_ms,
            ApiCallLog.error_message,
            ApiCallLog.created_at,
        ).where(ApiCallLog.created_at >= today_start)
    )).all()

    records = [
        {
            "service": r.service,
            "status": r.status,
            "response_time_ms": r.response_time_ms,
            "error_message": r.error_message,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]

    health = compute_service_health(records)

    summary = {"green": 0, "yellow": 0, "red": 0, "total": 0}
    for svc_health in health.values():
        summary[svc_health["status"]] += 1
        summary["total"] += 1

    return {
        "services": health,
        "summary": summary,
        "broker": {
            "mode": settings.broker_mode,
            "status": "green",
        },
    }


@router.get("/errors")
async def system_errors(db: AsyncSession = Depends(get_db), limit: int = 50):
    """Recent API call errors."""
    rows = (await db.execute(
        select(ApiCallLog)
        .where(ApiCallLog.status != "success")
        .order_by(ApiCallLog.created_at.desc())
        .limit(limit)
    )).scalars().all()

    return {
        "errors": [
            {
                "timestamp": r.created_at.isoformat() if r.created_at else None,
                "service": r.service,
                "endpoint": r.endpoint,
                "status": r.status,
                "error_message": r.error_message,
                "symbol": r.symbol,
                "response_time_ms": r.response_time_ms,
            }
            for r in rows
        ]
    }


@router.get("/trend")
async def system_trend(db: AsyncSession = Depends(get_db), days: int = 7):
    """Daily success rates per service for the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (await db.execute(
        select(
            cast(ApiCallLog.created_at, Date).label("day"),
            ApiCallLog.service,
            func.count().label("total"),
            func.sum(case((ApiCallLog.status == "success", 1), else_=0)).label("successes"),
        )
        .where(ApiCallLog.created_at >= cutoff)
        .group_by("day", ApiCallLog.service)
        .order_by("day")
    )).all()

    # Build response
    days_list = []
    services_data = {}
    day_set = set()

    for r in rows:
        day_str = r.day.isoformat() if hasattr(r.day, 'isoformat') else str(r.day)
        day_set.add(day_str)
        if r.service not in services_data:
            services_data[r.service] = {}
        pct = round(r.successes / r.total * 100) if r.total > 0 else 100
        services_data[r.service][day_str] = pct

    days_list = sorted(day_set)

    # Fill in missing days with 100 (no calls = no failures)
    trend = {}
    for svc, day_pcts in services_data.items():
        trend[svc] = [day_pcts.get(d, None) for d in days_list]

    return {
        "days": days_list,
        "services": trend,
    }
```

- [ ] **Step 2: Register router and add /system page route in main.py**

In `src/scorched/main.py`:

Add to the imports line:
```python
from .api import broker_status, costs, market, playbook, portfolio, recommendations, strategy, system, trades
```

After the broker_status router line, add:
```python
app.include_router(system.router, prefix="/api/v1")
```

After the strategy page route, add:
```python
@app.get("/system")
async def system_page():
    return FileResponse(STATIC_DIR / "system.html")
```

Add cleanup to the lifespan function — after the portfolio seeding block and before the MCP session manager:
```python
    # Cleanup old API call log records (>30 days)
    async with AsyncSessionLocal() as db:
        from .api_tracker import cleanup_old_records
        await cleanup_old_records(db)
```

- [ ] **Step 3: Verify app imports cleanly**

Run: `cd /home/ubuntu/tradebot && python3 -c "from scorched.api.system import router; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Run all tests**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/ -v 2>&1 | tail -10`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/scorched/api/system.py src/scorched/main.py
git commit -m "feat: add system health API endpoints and /system page route"
```

---

## Task 4: Instrument Research Pipeline with Tracker

**Files:**
- Modify: `src/scorched/services/research.py`
- Modify: `src/scorched/services/finnhub_data.py`
- Modify: `src/scorched/services/recommender.py`

This is the largest task — wrapping every external API call site with `track_call()`. The pattern is the same everywhere:

1. Each sync fetch function accepts an optional `tracker: ApiCallTracker | None = None` parameter
2. Inside the per-symbol loop, wrap the API call with `with track_call(tracker, service, endpoint, symbol=symbol):`
3. The existing try/except stays — `track_call` catches, records, and re-raises
4. The async wrapper passes the tracker through
5. The recommender creates one tracker, passes it to all fetches, then calls `tracker.flush(db)` after the gather

- [ ] **Step 1: Add tracker to research.py sync functions**

For each sync function in `research.py`, add the tracker parameter and wrap API calls. The key change is adding `tracker: ApiCallTracker | None = None` as the last parameter, then wrapping the yfinance/requests call inside `with track_call(tracker, ...) if tracker else nullcontext():`.

Since there are many functions, use this pattern. For `_fetch_price_data_sync`:

```python
def _fetch_price_data_sync(symbols: list[str], tracker=None) -> dict:
    result = {}
    for symbol in symbols:
        try:
            if tracker:
                from .api_tracker_helper import track_call
                with track_call(tracker, "yfinance", "history", symbol=symbol):
                    ticker = yf.Ticker(symbol)
                    hist = ticker.history(period="1y")
                    info = ticker.info
            else:
                ticker = yf.Ticker(symbol)
                hist = ticker.history(period="1y")
                info = ticker.info
            # ... rest of function unchanged
```

**However**, to avoid doubling every function body, create a small helper. Add at the top of `research.py`:

```python
from contextlib import nullcontext

def _api_ctx(tracker, service, endpoint, symbol=None):
    """Return a track_call context if tracker is provided, else nullcontext."""
    if tracker is None:
        return nullcontext()
    from ..api_tracker import track_call
    return track_call(tracker, service, endpoint, symbol=symbol)
```

Then in each sync function, add `tracker=None` parameter and wrap the per-symbol API call:

For `_fetch_price_data_sync`, wrap the yfinance calls:
```python
def _fetch_price_data_sync(symbols: list[str], tracker=None) -> dict:
    result = {}
    for symbol in symbols:
        try:
            with _api_ctx(tracker, "yfinance", "history", symbol):
                ticker = yf.Ticker(symbol)
                hist = ticker.history(period="1y")
                info = ticker.info
            # ... rest of processing (unchanged, outside the context manager)
```

Apply the same pattern to:
- `_fetch_news_sync` → service="yfinance", endpoint="news"
- `_fetch_earnings_surprise_sync` → service="yfinance", endpoint="earnings"
- `_fetch_edgar_insider_sync` → service="edgar", endpoint="submissions" (wrap the requests.get call)
- `_fetch_polygon_news_sync` → service="polygon", endpoint="news" (wrap the requests.get call)
- `_fetch_av_technicals_sync` → service="alpha_vantage", endpoint="rsi" (wrap the requests.get call)
- `_fetch_fred_macro_sync` → service="fred", endpoint="series" (wrap the fred.get_series call)
- `_fetch_options_data_sync` → service="yfinance", endpoint="options"
- `_fetch_market_context_sync` → service="yfinance", endpoint="market_context"
- `_fetch_momentum_screener_sync` → service="yfinance", endpoint="screener"
- `_fetch_opening_prices_sync` → service="yfinance", endpoint="opening_prices"

Update each corresponding async wrapper to accept and pass `tracker=None`:
```python
async def fetch_price_data(symbols: list[str], tracker=None) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_price_data_sync, symbols, tracker)
```

- [ ] **Step 2: Add tracker to finnhub_data.py**

In `fetch_analyst_consensus_sync`, add `tracker=None` parameter. Wrap the `client.recommendation_trends` and `client.price_target` calls:

```python
def fetch_analyst_consensus_sync(symbols, client, tracker=None):
    # ...
    for symbol in symbols:
        try:
            data = {}
            try:
                with _api_ctx(tracker, "finnhub", "recommendation_trends", symbol):
                    trends = client.recommendation_trends(symbol)
                # ... process trends
            except Exception as exc:
                logger.warning(...)

            try:
                with _api_ctx(tracker, "finnhub", "price_target", symbol):
                    pt = client.price_target(symbol)
                # ... process pt
            except Exception as exc:
                logger.warning(...)
```

Add the same `_api_ctx` helper at the top of `finnhub_data.py`:
```python
from contextlib import nullcontext

def _api_ctx(tracker, service, endpoint, symbol=None):
    if tracker is None:
        return nullcontext()
    from ..api_tracker import track_call
    return track_call(tracker, service, endpoint, symbol=symbol)
```

- [ ] **Step 3: Wire tracker through recommender**

In `src/scorched/services/recommender.py`, in `generate_recommendations`:

1. Import at top:
```python
from ..api_tracker import ApiCallTracker, track_call
```

2. Create tracker before the parallel fetch:
```python
    tracker = ApiCallTracker()
```

3. Pass `tracker=tracker` to all fetch calls in the gather:
```python
    (
        price_data, news_data, earnings_surprise, insider_activity,
        market_context, fred_macro, polygon_news, av_technicals
    ) = await asyncio.gather(
        fetch_price_data(research_symbols, tracker=tracker),
        fetch_news(research_symbols, tracker=tracker),
        fetch_earnings_surprise(research_symbols, tracker=tracker),
        fetch_edgar_insider(research_symbols, tracker=tracker),
        fetch_market_context(session_date, research_symbols, tracker=tracker),
        fetch_fred_macro(settings.fred_api_key, tracker=tracker),
        fetch_polygon_news(research_symbols, settings.polygon_api_key, tracker=tracker),
        fetch_av_technicals(screener_symbols, settings.alpha_vantage_api_key, tracker=tracker),
    )
```

4. Pass tracker to Finnhub fetch:
```python
    analyst_consensus = await asyncio.get_event_loop().run_in_executor(
        None, fetch_analyst_consensus_sync, research_symbols, finnhub_client, tracker
    )
```

5. Wrap Claude calls with track_call:
```python
    with track_call(tracker, "claude", "analysis"):
        call1_response = claude_call_with_retry(...)
```

```python
    with track_call(tracker, "claude", "decision"):
        call2_response = claude_call_with_retry(...)
```

```python
    if raw_recs:
        with track_call(tracker, "claude", "risk_review"):
            call3_response = claude_call_with_retry(...)
```

6. Flush tracker to DB after all calls are done (before the final commit):
```python
    await tracker.flush(db)
```

- [ ] **Step 4: Run all tests**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/ -v 2>&1 | tail -10`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/scorched/services/research.py src/scorched/services/finnhub_data.py src/scorched/services/recommender.py
git commit -m "feat: instrument all external API calls with tracker"
```

---

## Task 5: Dashboard Header Status Bar

**Files:**
- Modify: `src/scorched/static/dashboard.html`

- [ ] **Step 1: Add CSS for the status bar**

In the `<style>` section of `dashboard.html`, after the `.strategy-settings-btn` styles and before the closing `</style>`, add:

```css
  .api-health { display: flex; align-items: center; gap: 10px; font-size: 11px; }
  .api-health-badge {
    display: flex; align-items: center; gap: 5px;
    background: var(--surface); border: 1px solid var(--border);
    padding: 3px 10px; border-radius: 4px;
  }
  .api-health-badge .dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
  .api-health-badge .dot.green { background: var(--green); }
  .api-health-badge .dot.yellow { background: #eab308; }
  .api-health-badge .dot.red { background: var(--red); }
  .api-health-badge .count { font-weight: 600; }
  .api-health-badge .count.green { color: var(--green); }
  .api-health-badge .count.yellow { color: #eab308; }
  .api-health-badge .count.red { color: var(--red); }
  .api-health-badge .sep { color: var(--text-dim); }
  .broker-badge {
    display: flex; align-items: center; gap: 5px;
    background: var(--surface); border: 1px solid var(--border);
    padding: 3px 10px; border-radius: 4px;
  }
  .system-link {
    color: var(--amber); text-decoration: none; font-size: 10px;
    border: 1px solid var(--amber); padding: 2px 8px; border-radius: 3px;
    letter-spacing: 0.1em; transition: all 0.15s;
  }
  .system-link:hover { background: var(--amber); color: var(--bg); }
```

- [ ] **Step 2: Add status bar HTML to topbar**

In the topbar HTML, between `</div>` (closing topbar-left) and `<div class="topbar-right">`, insert:

```html
  <div class="api-health" id="api-health">
    <div class="api-health-badge">
      <span style="color:var(--text-dim)">APIs</span>
      <span class="dot green"></span><span class="count green" id="api-green">-</span>
      <span class="sep">/</span>
      <span class="dot yellow"></span><span class="count yellow" id="api-yellow">-</span>
      <span class="sep">/</span>
      <span class="dot red"></span><span class="count red" id="api-red">-</span>
    </div>
    <div class="broker-badge">
      <span style="color:var(--text-dim)">Broker</span>
      <span class="dot green" id="broker-dot"></span>
      <span style="color:var(--green);font-size:10px" id="broker-mode">-</span>
    </div>
    <a href="/system" class="system-link">SYSTEM</a>
  </div>
```

- [ ] **Step 3: Add JavaScript to fetch and render health**

In the `loadAll()` function, after the health check block and before the `Promise.allSettled` call, add:

```javascript
  // API health status bar
  try {
    const sh = await apiFetch('/system/health');
    document.getElementById('api-green').textContent = sh.summary.green || 0;
    document.getElementById('api-yellow').textContent = sh.summary.yellow || 0;
    document.getElementById('api-red').textContent = sh.summary.red || 0;
    // Broker
    const bmode = sh.broker?.mode || 'paper';
    const modeLabel = {paper:'PAPER',alpaca_paper:'ALPACA-PAPER',alpaca_live:'LIVE'}[bmode] || bmode.toUpperCase();
    document.getElementById('broker-mode').textContent = modeLabel;
    document.getElementById('broker-dot').className = 'dot ' + (sh.broker?.status || 'green');
  } catch(e) {
    document.getElementById('api-green').textContent = '?';
  }
```

- [ ] **Step 4: Commit**

```bash
git add src/scorched/static/dashboard.html
git commit -m "feat: add API health status bar to dashboard header"
```

---

## Task 6: System Operations Page

**Files:**
- Create: `src/scorched/static/system.html`

- [ ] **Step 1: Create the system page**

Create `src/scorched/static/system.html` — a full HTML page matching the existing dashboard's dark theme (IBM Plex Mono, same CSS variables, same topbar pattern). It should:

1. **Header:** "SYSTEM HEALTH" title with back link to dashboard, auto-refresh every 60 seconds
2. **API Cards Grid:** 4-column grid, 2 rows. One card per service (yfinance, FRED, Polygon, Finnhub, Alpha Vantage, EDGAR, Claude API, Alpaca). Each card shows:
   - Service name
   - Success rate % (large number, color-coded)
   - Call count (success/total)
   - Avg response time
   - Left border color matching status
3. **Recent Errors:** Scrollable list of last 50 errors from `/api/v1/system/errors`. Each row: timestamp, service badge, error message, symbol. Color-coded.
4. **7-Day Trend:** Grid with one row per service, one column per day. Cells show daily success % with background color (green >= 90%, yellow 50-89%, red < 50%, gray = no data).

The page fetches from three endpoints on load:
- `GET /api/v1/system/health` → cards + summary
- `GET /api/v1/system/errors` → error log
- `GET /api/v1/system/trend` → 7-day grid

Follow the exact same patterns as `dashboard.html`: embedded CSS/JS, `apiFetch()` helper, `esc()` for XSS safety, skeleton loaders, IBM Plex Mono font.

The page should be self-contained (single HTML file with all CSS and JS inline) matching the dark theme with amber accents.

- [ ] **Step 2: Verify page loads**

Run: `docker compose up -d --build --force-recreate tradebot`
Then visit `http://100.77.184.61:8000/system` and verify it loads with placeholder data (the API may return empty results until the next trading day populates data).

- [ ] **Step 3: Commit**

```bash
git add src/scorched/static/system.html
git commit -m "feat: add /system operations page with API health cards, errors, and trend"
```

---

## Task 7: Documentation + Rebuild

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md**

Add to Key Files table:
```
| `src/scorched/api_tracker.py` | API call tracking — sync recorder + health aggregation |
| `src/scorched/api/system.py` | System health endpoints: /system/health, /system/errors, /system/trend |
```

Add to Architecture section under the cron schedule:
```
**System Health:** All external API calls are tracked in `api_call_log` table. Dashboard header shows R/Y/G summary. `/system` page has full operational detail. Records auto-cleaned after 30 days.
```

- [ ] **Step 2: Rebuild Docker and push**

```bash
docker compose up -d --build --force-recreate tradebot
git add CLAUDE.md
git commit -m "docs: add system health tracking to documentation"
git push origin main
```

---

## Summary

| Task | What | Effort |
|------|------|--------|
| 1 | ApiCallLog model + Alembic migration | Small |
| 2 | ApiCallTracker — recording + health aggregation + tests | Medium |
| 3 | System health API endpoints + router registration | Medium |
| 4 | Instrument all fetch functions with tracker | Large (many files, repetitive) |
| 5 | Dashboard header status bar | Small (HTML/CSS/JS) |
| 6 | System operations page (full HTML) | Medium-Large (new page) |
| 7 | Documentation + rebuild | Small |
