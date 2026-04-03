from datetime import timedelta

from ..tz import market_today

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import TokenUsage

router = APIRouter(prefix="/costs", tags=["costs"])


@router.get("")
async def get_cost_summary(db: AsyncSession = Depends(get_db)):
    today = market_today()
    day_7 = today - timedelta(days=7)
    day_30 = today - timedelta(days=30)

    rows = (await db.execute(select(TokenUsage))).scalars().all()

    def _summarize(entries):
        total_cost = sum(float(r.estimated_cost_usd) for r in entries)
        by_type: dict[str, dict] = {}
        for r in entries:
            ct = r.call_type
            if ct not in by_type:
                by_type[ct] = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cost_usd": 0.0}
            by_type[ct]["input_tokens"] += r.input_tokens
            by_type[ct]["output_tokens"] += r.output_tokens
            by_type[ct]["thinking_tokens"] += r.thinking_tokens
            by_type[ct]["cost_usd"] += float(r.estimated_cost_usd)
        return {"total_cost_usd": round(total_cost, 6), "by_call_type": by_type}

    rows_7d = [r for r in rows if r.created_at.date() >= day_7]
    rows_30d = [r for r in rows if r.created_at.date() >= day_30]

    # Today's session breakdown
    today_rows = [r for r in rows if r.created_at.date() == today]

    return {
        "today": _summarize(today_rows),
        "last_7_days": _summarize(rows_7d),
        "last_30_days": _summarize(rows_30d),
        "all_time": _summarize(rows),
    }


@router.get("/sessions")
async def get_cost_by_session(limit: int = 14, db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(
                TokenUsage.session_id,
                func.sum(TokenUsage.estimated_cost_usd).label("total_cost"),
                func.sum(TokenUsage.input_tokens).label("input_tokens"),
                func.sum(TokenUsage.output_tokens).label("output_tokens"),
                func.sum(TokenUsage.thinking_tokens).label("thinking_tokens"),
                func.min(TokenUsage.created_at).label("created_at"),
            )
            .where(TokenUsage.session_id.isnot(None))
            .group_by(TokenUsage.session_id)
            .order_by(func.min(TokenUsage.created_at).desc())
            .limit(limit)
        )
    ).all()

    return [
        {
            "session_id": r.session_id,
            "total_cost_usd": round(float(r.total_cost), 6),
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "thinking_tokens": r.thinking_tokens,
            "date": r.created_at.date().isoformat(),
        }
        for r in rows
    ]
