"""Weekly trade reflection — reviews past outcomes to extract learnings."""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from ..tz import market_today

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api_tracker import ApiCallTracker
from ..cost import record_usage
from ..models import (
    Playbook,
    Portfolio,
    Position,
    RecommendationSession,
    TradeHistory,
    TradeRecommendation,
)
from .claude_client import MODEL, call_weekly_reflection, parse_json_response

logger = logging.getLogger(__name__)


async def generate_weekly_reflection(db: AsyncSession) -> dict:
    """Generate a weekly reflection on trade outcomes.

    Pulls: all trades from the past 7 days, the recommendations that led to them,
    current portfolio state, and compares predicted outcomes vs actual.
    """
    today = market_today()
    week_ago = today - timedelta(days=7)
    cutoff_dt = datetime.combine(week_ago, datetime.min.time())

    tracker = ApiCallTracker()

    # 1. Get all sells in the past 7 days
    sells_result = await db.execute(
        select(TradeHistory)
        .where(
            TradeHistory.action == "sell",
            TradeHistory.executed_at >= cutoff_dt,
        )
        .order_by(TradeHistory.executed_at.desc())
    )
    recent_sells = list(sells_result.scalars().all())

    # 2. Get all buys in the past 7 days
    buys_result = await db.execute(
        select(TradeHistory)
        .where(
            TradeHistory.action == "buy",
            TradeHistory.executed_at >= cutoff_dt,
        )
        .order_by(TradeHistory.executed_at.desc())
    )
    recent_buys = list(buys_result.scalars().all())

    # 3. For each sell, find the original buy recommendation
    sell_details = []
    for sell in recent_sells:
        detail = {
            "symbol": sell.symbol,
            "shares": float(sell.shares),
            "sell_price": float(sell.execution_price),
            "sell_date": sell.executed_at.strftime("%Y-%m-%d"),
            "realized_gain": float(sell.realized_gain) if sell.realized_gain else None,
            "tax_category": sell.tax_category,
        }

        # Find original recommendation reasoning
        if sell.recommendation_id:
            rec = await db.get(TradeRecommendation, sell.recommendation_id)
            if rec:
                detail["original_reasoning"] = rec.reasoning
                detail["original_confidence"] = rec.confidence
                detail["original_key_risks"] = rec.key_risks

        sell_details.append(detail)

    # 4. Get recommendations from the past 7 days that were NOT acted on
    sessions_result = await db.execute(
        select(RecommendationSession)
        .where(RecommendationSession.session_date >= week_ago)
        .order_by(RecommendationSession.session_date.desc())
    )
    sessions = list(sessions_result.scalars().all())

    skipped_recs = []
    for session in sessions:
        for rec in session.recommendations:
            if rec.status == "rejected" and rec.action == "buy":
                skipped_recs.append({
                    "symbol": rec.symbol,
                    "action": rec.action,
                    "suggested_price": float(rec.suggested_price),
                    "reasoning": rec.reasoning,
                    "date": session.session_date.isoformat(),
                })

    # 5. Get current prices for skipped recommendations to see if they would have been profitable
    skipped_symbols = list({r["symbol"] for r in skipped_recs})
    current_prices = {}
    if skipped_symbols:
        import yfinance as yf

        def _fetch_prices():
            prices = {}
            for sym in skipped_symbols:
                try:
                    ticker = yf.Ticker(sym)
                    hist = ticker.history(period="1d")
                    if not hist.empty:
                        prices[sym] = float(hist["Close"].iloc[-1])
                except Exception:
                    pass
            return prices

        loop = asyncio.get_running_loop()
        current_prices = await loop.run_in_executor(None, _fetch_prices)

    for rec in skipped_recs:
        sym = rec["symbol"]
        if sym in current_prices:
            suggested = rec["suggested_price"]
            current = current_prices[sym]
            rec["current_price"] = current
            rec["would_have_pct"] = round((current - suggested) / suggested * 100, 2)

    # 6. Get current portfolio state
    portfolio = (await db.execute(select(Portfolio))).scalars().first()
    positions = list((await db.execute(select(Position))).scalars().all())

    # 7. Get current playbook
    playbook = (await db.execute(
        select(Playbook).order_by(Playbook.updated_at.desc()).limit(1)
    )).scalars().first()

    # 8. Build prompt
    lines = [
        f"## Week in Review: {week_ago.isoformat()} to {today.isoformat()}",
        "",
    ]

    # Portfolio summary
    if portfolio:
        lines.append(f"Portfolio cash: ${float(portfolio.cash_balance):,.2f}")
        lines.append(f"Starting capital: ${float(portfolio.starting_capital):,.2f}")
        lines.append(f"Open positions: {len(positions)}")
        lines.append("")

    # Sells (completed trades)
    if sell_details:
        lines.append("## Completed Trades (Sells)")
        total_realized = 0
        wins = 0
        losses = 0
        for s in sell_details:
            gain = s.get("realized_gain")
            gain_str = f"${gain:+,.2f}" if gain is not None else "unknown"
            lines.append(f"- {s['symbol']}: sold {s['shares']} shares @ ${s['sell_price']:.2f} on {s['sell_date']} | P&L: {gain_str}")
            if s.get("original_reasoning"):
                lines.append(f"  Original thesis: {s['original_reasoning'][:300]}")
            if s.get("original_key_risks"):
                lines.append(f"  Key risks identified: {s['original_key_risks'][:200]}")
            if gain is not None:
                total_realized += gain
                if gain > 0:
                    wins += 1
                else:
                    losses += 1
        lines.append(f"\nTotal realized P&L this week: ${total_realized:+,.2f}")
        lines.append(f"Win/Loss: {wins}W / {losses}L")
        lines.append("")
    else:
        lines.append("## No sells this week.")
        lines.append("")

    # Buys
    if recent_buys:
        lines.append("## New Positions Opened")
        for b in recent_buys:
            lines.append(f"- {b.symbol}: bought {float(b.shares)} shares @ ${float(b.execution_price):.2f} on {b.executed_at.strftime('%Y-%m-%d')}")
        lines.append("")

    # Skipped recommendations
    if skipped_recs:
        lines.append("## Recommendations NOT Acted On (rejected/skipped)")
        for r in skipped_recs:
            current_str = ""
            if "current_price" in r and "would_have_pct" in r:
                current_str = f" | Now: ${r['current_price']:.2f} ({r['would_have_pct']:+.1f}%)"
            lines.append(f"- {r['symbol']} ({r['date']}): suggested @ ${r['suggested_price']:.2f}{current_str}")
            lines.append(f"  Reasoning: {r['reasoning'][:200]}")
        lines.append("")

    # Current playbook excerpt
    if playbook:
        lines.append("## Current Playbook (excerpt)")
        lines.append(playbook.content[:1500])
        lines.append("")

    prompt_text = "\n".join(lines)

    # 9. Call Claude
    logger.info("Weekly reflection: calling Claude with %d chars", len(prompt_text))
    response, raw_text = await call_weekly_reflection(prompt_text, tracker=tracker)

    # Record cost
    usage = response.usage
    await record_usage(
        db,
        session_id=None,
        call_type="weekly_reflection",
        model=MODEL,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )

    # 10. Parse response
    parsed = parse_json_response(raw_text)
    if not parsed:
        logger.warning("Weekly reflection: failed to parse JSON, returning raw text")
        parsed = {
            "learnings": [raw_text[:500]],
            "pattern_detected": "Unable to parse structured response",
            "strategy_adjustment": "none needed",
            "grade": "N/A",
            "raw_response": raw_text,
        }

    # 11. Append learnings to playbook
    if playbook and parsed.get("learnings"):
        learnings_text = "\n".join(f"- {l}" for l in parsed["learnings"])
        grade = parsed.get("grade", "N/A")
        adjustment = parsed.get("strategy_adjustment", "none needed")
        reflection_section = (
            f"\n\n## Weekly Reflection ({today.isoformat()}, Grade: {grade})\n"
            f"### Learnings\n{learnings_text}\n"
            f"### Pattern: {parsed.get('pattern_detected', 'none')}\n"
            f"### Adjustment: {adjustment}\n"
        )
        playbook.content = playbook.content + reflection_section
        playbook.version += 1
        logger.info("Weekly reflection: appended to playbook (version %d)", playbook.version)

    await tracker.flush(db)
    await db.commit()

    result = {
        "status": "ok",
        "week": f"{week_ago.isoformat()} to {today.isoformat()}",
        "sells_reviewed": len(sell_details),
        "buys_reviewed": len(recent_buys),
        "skipped_recs_reviewed": len(skipped_recs),
        "reflection": parsed,
    }
    logger.info("Weekly reflection complete: %d sells, %d buys, grade=%s",
                len(sell_details), len(recent_buys), parsed.get("grade", "N/A"))
    return result
