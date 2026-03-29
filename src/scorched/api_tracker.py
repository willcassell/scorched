"""API call tracker — records external service calls and computes health."""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

SERVICES = [
    "yfinance",
    "fred",
    "polygon",
    "finnhub",
    "alpha_vantage",
    "edgar",
    "claude",
    "alpaca",
]


class ApiCallTracker:
    """Accumulates API call records in-memory for later bulk flush to DB."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def flush(self, db: AsyncSession) -> None:
        """Bulk-insert accumulated records into ApiCallLog, then clear."""
        if not self.records:
            return
        # Lazy import to avoid circular imports
        from scorched.models import ApiCallLog

        objects = [
            ApiCallLog(
                service=r["service"],
                endpoint=r["endpoint"],
                status=r["status"],
                response_time_ms=r["response_time_ms"],
                error_message=r.get("error_message"),
                symbol=r.get("symbol"),
            )
            for r in self.records
        ]
        db.add_all(objects)
        await db.commit()
        self.records.clear()


@contextmanager
def track_call(
    tracker: ApiCallTracker,
    service: str,
    endpoint: str,
    *,
    symbol: str | None = None,
):
    """Context manager that measures and records an API call.

    On success: status="success"
    On TimeoutError: status="timeout", re-raises
    On exception containing "429" or "rate": status="rate_limited", re-raises
    On other exception: status="error", re-raises
    """
    status = "success"
    error_message: str | None = None
    start = time.monotonic()

    try:
        yield
    except TimeoutError as exc:
        status = "timeout"
        error_message = str(exc)[:500]
        raise
    except Exception as exc:
        exc_str = str(exc)
        if "429" in exc_str or "rate" in exc_str.lower():
            status = "rate_limited"
        else:
            status = "error"
        error_message = exc_str[:500]
        raise
    finally:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        tracker.records.append(
            {
                "service": service,
                "endpoint": endpoint,
                "status": status,
                "response_time_ms": elapsed_ms,
                "error_message": error_message,
                "symbol": symbol,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )


def compute_service_health(records: list[dict[str, Any]]) -> dict[str, dict]:
    """Aggregate call records into per-service health summaries.

    Returns:
        {service: {status, today_success, today_total, today_pct,
                    avg_response_ms, last_error, last_error_at}}

    Status thresholds: green >= 90%, yellow >= 50%, red < 50%.
    """
    if not records:
        return {}

    # Group by service
    by_service: dict[str, list[dict]] = {}
    for rec in records:
        by_service.setdefault(rec["service"], []).append(rec)

    result: dict[str, dict] = {}
    for service, calls in by_service.items():
        total = len(calls)
        successes = sum(1 for c in calls if c["status"] == "success")
        pct = (successes / total) * 100.0 if total else 0.0

        avg_ms = sum(c["response_time_ms"] for c in calls) / total if total else 0.0

        # Find last error
        errors = [c for c in calls if c["status"] != "success"]
        last_error = errors[-1]["error_message"] if errors else None
        last_error_at = errors[-1]["created_at"] if errors else None

        if pct >= 90.0:
            status = "green"
        elif successes > 0:
            status = "yellow"
        else:
            status = "red"

        result[service] = {
            "status": status,
            "today_success": successes,
            "today_total": total,
            "today_pct": pct,
            "avg_response_ms": round(avg_ms, 1),
            "last_error": last_error,
            "last_error_at": last_error_at,
        }

    return result


async def cleanup_old_records(db: AsyncSession, days: int = 30) -> None:
    """Delete ApiCallLog records older than N days."""
    from scorched.models import ApiCallLog

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    await db.execute(delete(ApiCallLog).where(ApiCallLog.created_at < cutoff))
    await db.commit()
