"""EOD performance review: analyzes how today's recommendations performed and updates the playbook."""
import asyncio
import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..cost import record_usage
from ..models import Position, RecommendationSession, TradeHistory, TradeRecommendation
from .claude_client import MODEL, call_eod_review as _call_eod_review, call_position_review as _call_position_review
from .playbook import get_playbook
from .position_mgmt import build_position_review_prompt
from .research import fetch_market_eod, fetch_price_data

logger = logging.getLogger(__name__)


async def _get_execution_for_rec(db: AsyncSession, recommendation_id: int) -> TradeHistory | None:
    result = await db.execute(
        select(TradeHistory)
        .where(TradeHistory.recommendation_id == recommendation_id)
        .order_by(TradeHistory.executed_at.desc())
        .limit(1)
    )
    return result.scalars().first()


def _build_review_context(
    review_date: date,
    session: RecommendationSession,
    recommendations: list[TradeRecommendation],
    executions: dict[int, TradeHistory],
    positions: list[Position],
    eod_prices: dict,
    market_eod: dict,
) -> str:
    lines = [f"Date: {review_date}"]

    # Market summary
    lines.append("\n## Market Performance Today")
    for label, data in market_eod.get("indices", {}).items():
        pct = data.get("change_pct") or 0
        sign = "+" if pct >= 0 else ""
        lines.append(f"  {label}: {sign}{pct:.2f}%")
    sector_parts = []
    for sym, data in market_eod.get("sectors", {}).items():
        pct = data.get("change_pct") or 0
        sign = "+" if pct >= 0 else ""
        sector_parts.append(f"{data['label']} {sign}{pct:.1f}%")
    if sector_parts:
        lines.append("  Sectors: " + ", ".join(sector_parts))

    # Morning analysis (strip thinking block, truncate)
    if session.analysis_text:
        analysis = session.analysis_text
        if "[ANALYSIS]" in analysis:
            analysis = analysis.split("[ANALYSIS]", 1)[1].strip()
        if len(analysis) > 1500:
            analysis = analysis[:1500] + "... [truncated]"
        lines.append(f"\n## Morning Analysis Summary\n{analysis}")

    # Recommendations and intraday outcomes
    lines.append("\n## Today's Recommendations vs. EOD Prices")
    if not recommendations:
        lines.append("  No recommendations were made today.")
    for rec in recommendations:
        eod_price = (eod_prices.get(rec.symbol) or {}).get("current_price")
        execution = executions.get(rec.id)
        rec_price = float(rec.suggested_price)

        if execution:
            entry = float(execution.execution_price)
            confirmed_note = f"confirmed @ ${entry:.2f}"
            if eod_price and rec.action == "buy":
                move_pct = (eod_price - entry) / entry * 100
                sign = "+" if move_pct >= 0 else ""
                eod_note = f" → EOD ${eod_price:.2f} ({sign}{move_pct:.1f}% from entry)"
            elif eod_price:
                eod_note = f" → EOD ${eod_price:.2f}"
            else:
                eod_note = " → EOD price unavailable"
        elif rec.status == "rejected":
            confirmed_note = "REJECTED"
            if eod_price:
                move_pct = (eod_price - rec_price) / rec_price * 100
                sign = "+" if move_pct >= 0 else ""
                eod_note = f" → EOD ${eod_price:.2f} ({sign}{move_pct:.1f}% from rec price — would have been)"
            else:
                eod_note = ""
        else:
            confirmed_note = "not executed (pending)"
            eod_note = f" → EOD ${eod_price:.2f}" if eod_price else ""

        lines.append(
            f"  {rec.action.upper()} {rec.symbol} @ ${rec_price:.2f} | "
            f"confidence: {rec.confidence} | {confirmed_note}{eod_note}"
        )
        lines.append(f"    Thesis: {rec.reasoning[:250]}")

    # Portfolio at EOD
    lines.append("\n## Open Positions at EOD")
    if positions:
        for pos in positions:
            cost = float(pos.avg_cost_basis)
            shares = float(pos.shares)
            eod_price = (eod_prices.get(pos.symbol) or {}).get("current_price")
            if eod_price:
                unreal_pct = (eod_price - cost) / cost * 100
                unreal_usd = (eod_price - cost) * shares
                sign = "+" if unreal_pct >= 0 else ""
                lines.append(
                    f"  {pos.symbol}: {shares:.0f} sh | cost ${cost:.2f} | "
                    f"EOD ${eod_price:.2f} | {sign}${unreal_usd:,.2f} ({sign}{unreal_pct:.1f}%)"
                )
            else:
                lines.append(f"  {pos.symbol}: {shares:.0f} sh | cost ${cost:.2f} | EOD price unavailable")
    else:
        lines.append("  No open positions.")

    return "\n".join(lines)


async def run_eod_review(db: AsyncSession, review_date: date | None = None) -> dict:
    """
    End-of-day performance review. Fetches today's recommendation outcomes,
    compares entry prices against EOD closes, calls Claude to distill learnings,
    and updates the playbook. Returns a summary dict.
    """
    if review_date is None:
        review_date = date.today()

    # Get today's session (may not exist if market was closed or no trades)
    session = (
        await db.execute(
            select(RecommendationSession)
            .where(RecommendationSession.session_date == review_date)
        )
    ).scalars().first()

    if session is None:
        logger.info("No recommendation session for %s — skipping EOD review", review_date)
        return {"status": "skipped", "reason": "No recommendation session found for this date"}

    recommendations = list(session.recommendations)  # loaded via selectin
    positions = list((await db.execute(select(Position))).scalars().all())

    # Collect symbols to price
    all_symbols = list({rec.symbol for rec in recommendations} | {pos.symbol for pos in positions})

    # Find confirmed executions for each recommendation
    executions: dict[int, TradeHistory] = {}
    for rec in recommendations:
        execution = await _get_execution_for_rec(db, rec.id)
        if execution:
            executions[rec.id] = execution

    # Fetch EOD prices + market context in parallel
    async def _empty_prices() -> dict:
        return {}

    eod_prices, market_eod = await asyncio.gather(
        fetch_price_data(all_symbols) if all_symbols else _empty_prices(),
        fetch_market_eod(review_date),
    )

    context = _build_review_context(
        review_date=review_date,
        session=session,
        recommendations=recommendations,
        executions=executions,
        positions=positions,
        eod_prices=eod_prices,
        market_eod=market_eod,
    )

    playbook = await get_playbook(db)

    user_content = (
        f"{context}\n\n"
        f"## Current Playbook\n{playbook.content}\n\n"
        f"Review today's outcomes against the playbook and produce an updated version "
        f"that incorporates what was learned today."
    )

    logger.info("EOD review: calling Claude to update playbook (date=%s)", review_date)
    response, updated_content = _call_eod_review(user_content)

    await record_usage(
        db,
        session_id=session.id,
        call_type="eod_review",
        model=MODEL,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    playbook.content = updated_content
    playbook.version += 1
    await db.commit()
    await db.refresh(playbook)

    logger.info("EOD review complete — playbook updated to v%d", playbook.version)

    # ── Call 4: Position management review ───────────────────────────────────
    if positions:
        logger.info("Call 4: position management review for %d positions", len(positions))
        pos_list = []
        for p in positions:
            cp = float((eod_prices.get(p.symbol) or {}).get("current_price", float(p.avg_cost_basis)))
            acb = float(p.avg_cost_basis)
            pos_list.append({
                "symbol": p.symbol,
                "shares": float(p.shares),
                "avg_cost_basis": acb,
                "current_price": cp,
                "unrealized_gain_pct": round((cp - acb) / acb * 100, 1) if acb > 0 else 0,
                "days_held": (review_date - p.first_purchase_date).days,
            })

        market_summary = context[:500] if context else "Market data unavailable"
        pos_prompt = build_position_review_prompt(pos_list, market_summary)

        pos_response, pos_text = _call_position_review(pos_prompt)

        await record_usage(
            db,
            session_id=None,
            call_type="position_mgmt",
            model=MODEL,
            input_tokens=pos_response.usage.input_tokens,
            output_tokens=pos_response.usage.output_tokens,
        )

        logger.info("Position management review: %s", pos_text[:200])

    return {
        "status": "completed",
        "review_date": review_date.isoformat(),
        "playbook_version": playbook.version,
        "recommendations_reviewed": len(recommendations),
        "positions_tracked": len(positions),
    }
