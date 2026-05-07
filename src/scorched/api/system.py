"""System health, error log, and trend endpoints."""
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy import Date, Integer, case

from ..api_tracker import compute_service_health, SERVICES
from ..broker.pending_fills import get_pending_fills_summary
from ..config import settings
from ..database import get_db
from ..models import ApiCallLog
from ..services.gate_decisions import (
    list_recent_gate_decisions,
    summarize_gate_attribution,
)
from ..tz import market_today
from .deps import require_owner_pin

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health", dependencies=[Depends(require_owner_pin)])
async def system_health(db: AsyncSession = Depends(get_db)):
    """Return per-service health based on today's api_call_log records."""
    today = market_today()
    result = await db.execute(
        select(ApiCallLog).where(
            cast(ApiCallLog.created_at, Date) == today
        )
    )
    rows = result.scalars().all()

    records = [
        {
            "service": r.service,
            "endpoint": r.endpoint,
            "status": r.status,
            "response_time_ms": r.response_time_ms,
            "error_message": r.error_message,
            "symbol": r.symbol,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]

    services = compute_service_health(records)

    green = sum(1 for v in services.values() if v["status"] == "green")
    yellow = sum(1 for v in services.values() if v["status"] == "yellow")
    red = sum(1 for v in services.values() if v["status"] == "red")
    total = len(services)

    broker = {
        "mode": settings.broker_mode,
        "status": "green",
    }

    return {
        "services": services,
        "summary": {"green": green, "yellow": yellow, "red": red, "total": total},
        "broker": broker,
    }


@router.get("/errors", dependencies=[Depends(require_owner_pin)])
async def system_errors(db: AsyncSession = Depends(get_db)):
    """Return last 50 non-success records from api_call_log, newest first."""
    result = await db.execute(
        select(ApiCallLog)
        .where(ApiCallLog.status != "success")
        .order_by(ApiCallLog.created_at.desc())
        .limit(50)
    )
    rows = result.scalars().all()

    errors = [
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

    return {"errors": errors}


@router.get("/trend", dependencies=[Depends(require_owner_pin)])
async def system_trend(db: AsyncSession = Depends(get_db)):
    """Return daily success % per service for the last 7 days."""
    cutoff = market_today() - timedelta(days=6)

    day_col = cast(ApiCallLog.created_at, Date).label("day")
    success_col = func.sum(
        case((ApiCallLog.status == "success", 1), else_=0)
    ).label("successes")
    total_col = func.count().label("total")

    result = await db.execute(
        select(day_col, ApiCallLog.service, success_col, total_col)
        .where(cast(ApiCallLog.created_at, Date) >= cutoff)
        .group_by(day_col, ApiCallLog.service)
        .order_by(day_col)
    )
    rows = result.all()

    # Build ordered list of days (last 7)
    days = [(cutoff + timedelta(days=i)).isoformat() for i in range(7)]

    # Build service → day → pct map
    pct_map: dict[str, dict[str, float]] = {}
    for row in rows:
        day_str = row.day.isoformat() if isinstance(row.day, date) else str(row.day)
        svc = row.service
        pct = round((row.successes / row.total) * 100.0, 1) if row.total else 0.0
        pct_map.setdefault(svc, {})[day_str] = pct

    services_out: dict[str, list[float | None]] = {}
    for svc in pct_map:
        services_out[svc] = [pct_map[svc].get(d) for d in days]

    return {"days": days, "services": services_out}


@router.get("/market-date")
async def get_market_date():
    """Return today's trading date in the market timezone."""
    return {"date": market_today().isoformat()}


@router.get("/gate-attribution", dependencies=[Depends(require_owner_pin)])
async def gate_attribution(
    days: int = Query(14, ge=1, le=90),
    sample_size: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
):
    """Group gate decisions in the last `days` by (phase, gate) with verdict counts.

    Operator dashboard: answers "which gate killed the most buys this week?"
    in one query before any prompt or strategy tuning.
    """
    rows = await summarize_gate_attribution(db, days=days, sample_size=sample_size)
    return {
        "lookback_days": days,
        "rows": [asdict(r) for r in rows],
    }


@router.get("/gate-decisions", dependencies=[Depends(require_owner_pin)])
async def gate_decisions_recent(
    limit: int = Query(100, ge=1, le=500),
    only_blocked: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Most recent gate decisions for forensic inspection of a session."""
    rows = await list_recent_gate_decisions(db, limit=limit, only_blocked=only_blocked)
    return {"decisions": rows}


@router.get("/pending-fills", dependencies=[Depends(require_owner_pin)])
async def pending_fills(db: AsyncSession = Depends(get_db)):
    """Snapshot of pending-fill state, including stale reservations.

    `stale_count` and `reserved_buy_stale_notional` should be 0 in steady
    state. Non-zero indicates the reconciler couldn't resolve a leak; the
    next process restart will release them via the lifecycle backstop.
    """
    return await get_pending_fills_summary(db)
