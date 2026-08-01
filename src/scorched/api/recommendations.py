from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import GenerateRecommendationsRequest, RecommendationsResponse, SessionDetail, SessionListItem
from ..services import recommender as recommender_svc
from .deps import require_owner_pin

router = APIRouter(prefix="/recommendations", tags=["recommendations"], dependencies=[Depends(require_owner_pin)])


@router.post("/generate", response_model=RecommendationsResponse, dependencies=[Depends(require_owner_pin)])
async def generate_recommendations(
    body: GenerateRecommendationsRequest,
    db: AsyncSession = Depends(get_db),
):
    return await recommender_svc.generate_recommendations(
        db,
        session_date=body.session_date,
        force=body.force,
    )


@router.get("", response_model=list[SessionListItem])
async def list_sessions(
    session_date: date | None = Query(None),
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    from decimal import Decimal
    from ..schemas import RecommendationItem

    rows = await recommender_svc.list_sessions(db, session_date=session_date, limit=limit)
    return [
        SessionListItem(
            id=r.id,
            session_date=r.session_date,
            recommendation_count=len(r.recommendations),
            created_at=r.created_at,
            recommendations=[
                RecommendationItem(
                    id=rec.id,
                    symbol=rec.symbol,
                    action=rec.action,
                    suggested_price=rec.suggested_price,
                    quantity=rec.quantity,
                    estimated_cost=(rec.suggested_price * rec.quantity).quantize(Decimal("0.01")),
                    reasoning=rec.reasoning,
                    confidence=rec.confidence,
                    key_risks=rec.key_risks,
                    status=rec.status,
                )
                for rec in r.recommendations
            ],
        )
        for r in rows
    ]


@router.get("/{session_id}/analysis")
async def get_session_analysis(session_id: int, db: AsyncSession = Depends(get_db)):
    from fastapi import HTTPException
    row = await recommender_svc.get_session(db, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": session_id, "analysis_text": row.analysis_text}


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(session_id: int, db: AsyncSession = Depends(get_db)):
    import json
    from decimal import Decimal
    from fastapi import HTTPException
    from ..schemas import RecommendationItem

    row = await recommender_svc.get_session(db, session_id)

    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    research_summary = ""
    if row.claude_response:
        try:
            research_summary = json.loads(row.claude_response).get("research_summary", "")
        except Exception:
            pass

    recs = [
        RecommendationItem(
            id=r.id,
            symbol=r.symbol,
            action=r.action,
            suggested_price=r.suggested_price,
            quantity=r.quantity,
            estimated_cost=(r.suggested_price * r.quantity).quantize(Decimal("0.01")),
            reasoning=r.reasoning,
            confidence=r.confidence,
            key_risks=r.key_risks,
            status=r.status,
        )
        for r in row.recommendations
    ]

    return SessionDetail(
        id=row.id,
        session_date=row.session_date,
        research_summary=research_summary,
        recommendations=recs,
        created_at=row.created_at,
    )
