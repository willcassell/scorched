"""Gate-decision recorder + reader helpers.

Every trade-pipeline gate (drawdown, cash-floor, holdings, position-cap,
sector-cap, circuit-breaker, drift, risk-review verdict) writes a row here so
operators can answer "which gate blocked the most buys this week?" without
trawling logs.

The recorder is **best-effort**. A failure to record MUST NOT raise into the
hot path — losing telemetry is preferable to losing a trade decision.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import AsyncSessionLocal
from ..models import GateDecision

logger = logging.getLogger(__name__)


# Phase identifiers — keep stable across releases for analytics joins.
PHASE_FILTER = "phase1_filter"          # candidate filters inside recommender.py
PHASE_RISK_REVIEW = "phase1_risk_review"  # Call 3 verdicts
PHASE_CIRCUIT = "phase1.5_circuit"      # cron-driven circuit breaker
PHASE_CONFIRM = "phase2_confirm"        # /trades/confirm + MCP confirm_trade


def _coerce_jsonable(value: Any) -> Any:
    """Convert Decimal/datetime/date into JSON-serializable values."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _coerce_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_coerce_jsonable(v) for v in value]
    return value


async def record_gate_decision(
    db: AsyncSession | None,
    *,
    symbol: str,
    action: str,
    phase: str,
    gate: str,
    passed: bool,
    reason: str | None = None,
    details: dict | None = None,
    session_id: int | None = None,
    recommendation_id: int | None = None,
) -> None:
    """Best-effort persistence of a single gate decision.

    Opens a dedicated short-lived session via `AsyncSessionLocal` so the
    decision is durable even when the surrounding hot-path transaction rolls
    back — the typical case, since a gate that REJECTS raises ValueError that
    would otherwise discard the record.

    The `db` parameter is accepted for API symmetry but ignored. Tests that
    need to inject a session can monkeypatch
    `scorched.services.gate_decisions.AsyncSessionLocal`.

    Any failure is logged and swallowed — recording must never raise into the
    hot path.
    """
    try:
        coerced_details = _coerce_jsonable(details) if details is not None else None
        if coerced_details is not None:
            # Surface non-serializable details to the log instead of letting
            # them pollute storage with implementation-specific repr().
            json.dumps(coerced_details)

        async with AsyncSessionLocal() as own_db:
            own_db.add(
                GateDecision(
                    session_id=session_id,
                    recommendation_id=recommendation_id,
                    symbol=symbol.upper() if symbol else "",
                    action=action,
                    phase=phase,
                    gate=gate,
                    passed=passed,
                    reason=reason,
                    details=coerced_details,
                )
            )
            await own_db.commit()
    except Exception:
        logger.exception(
            "Failed to record gate decision: phase=%s gate=%s symbol=%s passed=%s",
            phase, gate, symbol, passed,
        )


@dataclass
class GateAttributionRow:
    phase: str
    gate: str
    passed_count: int
    blocked_count: int
    sample_reasons: list[str]


async def summarize_gate_attribution(
    db: AsyncSession,
    *,
    days: int = 14,
    sample_size: int = 5,
) -> list[GateAttributionRow]:
    """Group decisions in the last `days` by (phase, gate) with verdict counts.

    Returns one row per (phase, gate) pair, with up to `sample_size` distinct
    rejection reasons so operators can see WHY each gate fired without paging
    through logs.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(
            GateDecision.phase,
            GateDecision.gate,
            GateDecision.passed,
            func.count(GateDecision.id).label("n"),
        )
        .where(GateDecision.created_at >= cutoff)
        .group_by(GateDecision.phase, GateDecision.gate, GateDecision.passed)
    )
    result = await db.execute(stmt)

    counts: dict[tuple[str, str], dict[str, int]] = {}
    for phase, gate, passed, n in result.all():
        bucket = counts.setdefault((phase, gate), {"passed": 0, "blocked": 0})
        bucket["passed" if passed else "blocked"] += int(n)

    sample_stmt = (
        select(
            GateDecision.phase,
            GateDecision.gate,
            GateDecision.reason,
        )
        .where(
            GateDecision.created_at >= cutoff,
            GateDecision.passed.is_(False),
            GateDecision.reason.is_not(None),
        )
        .order_by(GateDecision.created_at.desc())
    )
    sample_result = await db.execute(sample_stmt)
    samples: dict[tuple[str, str], list[str]] = {}
    for phase, gate, reason in sample_result.all():
        bucket = samples.setdefault((phase, gate), [])
        if reason and reason not in bucket and len(bucket) < sample_size:
            bucket.append(reason)

    rows: list[GateAttributionRow] = []
    for (phase, gate), c in counts.items():
        rows.append(
            GateAttributionRow(
                phase=phase,
                gate=gate,
                passed_count=c["passed"],
                blocked_count=c["blocked"],
                sample_reasons=samples.get((phase, gate), []),
            )
        )
    rows.sort(key=lambda r: (-r.blocked_count, r.phase, r.gate))
    return rows


async def list_recent_gate_decisions(
    db: AsyncSession,
    *,
    limit: int = 100,
    only_blocked: bool = False,
) -> list[dict]:
    """Most recent decisions for forensic inspection of a specific session."""
    stmt = select(GateDecision).order_by(GateDecision.created_at.desc())
    if only_blocked:
        stmt = stmt.where(GateDecision.passed.is_(False))
    stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": r.id,
            "session_id": r.session_id,
            "recommendation_id": r.recommendation_id,
            "symbol": r.symbol,
            "action": r.action,
            "phase": r.phase,
            "gate": r.gate,
            "passed": r.passed,
            "reason": r.reason,
            "details": r.details,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
