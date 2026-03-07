"""Playbook service: reads and updates the bot's living strategy document."""
import logging
from datetime import date
from decimal import Decimal

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Playbook, TradeHistory, TradeRecommendation

logger = logging.getLogger(__name__)

INITIAL_PLAYBOOK = """# Trading Playbook

## Strategy Overview
No trades have been made yet. Starting fresh with $100k simulated capital.

## What Has Worked
(No data yet)

## What Has Not Worked
(No data yet)

## Current Biases to Watch
(No data yet)

## Position Sizing Rules Learned
- Default: size positions at ~15-20% of portfolio per trade
- No single position should exceed 25% of total portfolio value

## Sectors / Themes to Favor
(No data yet)

## Sectors / Themes to Avoid
(No data yet)

## Notes
This playbook is updated each morning before recommendations are generated.
"""

UPDATE_SYSTEM_PROMPT = """You are maintaining a trading strategy playbook for a simulated stock portfolio. Your job is to review recent closed trade outcomes and update the playbook to reflect genuine learnings.

Be honest and specific. If a thesis was wrong, say so clearly. If a pattern is emerging, name it. The playbook should help future you make better decisions — not rationalize past ones.

Update the playbook by:
1. Noting what worked and why (was the thesis correct, or did you get lucky?)
2. Noting what didn't work and what the actual cause was
3. Updating sector/theme biases based on observed outcomes
4. Refining position sizing guidance if relevant
5. Flagging any recurring mistakes

Return ONLY the full updated playbook text. Preserve the existing structure but rewrite sections as needed. Do not wrap in markdown code blocks."""


async def get_playbook(db: AsyncSession) -> Playbook:
    """Get the current playbook, creating it if it doesn't exist."""
    row = (await db.execute(select(Playbook))).scalars().first()
    if row is None:
        row = Playbook(content=INITIAL_PLAYBOOK, version=1)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        logger.info("Initialized playbook")
    return row


async def _get_recent_closed_trades(db: AsyncSession, limit: int = 20) -> list[dict]:
    """Fetch recent sells with their original recommendation reasoning."""
    sells = (
        await db.execute(
            select(TradeHistory)
            .where(TradeHistory.action == "sell")
            .order_by(TradeHistory.executed_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    results = []
    for sell in sells:
        rec_reasoning = None
        if sell.recommendation_id:
            rec = (
                await db.execute(
                    select(TradeRecommendation).where(
                        TradeRecommendation.id == sell.recommendation_id
                    )
                )
            ).scalars().first()
            if rec:
                rec_reasoning = rec.reasoning

        # Also try to find the original buy recommendation for context
        results.append({
            "symbol": sell.symbol,
            "sell_date": sell.executed_at.date().isoformat(),
            "shares": float(sell.shares),
            "execution_price": float(sell.execution_price),
            "realized_gain": float(sell.realized_gain) if sell.realized_gain else 0.0,
            "realized_gain_pct": (
                round(float(sell.realized_gain) / (float(sell.execution_price) * float(sell.shares) - float(sell.realized_gain)) * 100, 2)
                if sell.realized_gain and sell.execution_price and sell.shares
                else 0.0
            ),
            "tax_category": sell.tax_category,
            "sell_reasoning": rec_reasoning,
        })

    return results


def _format_closed_trades_for_prompt(closed_trades: list[dict]) -> str:
    if not closed_trades:
        return "No closed trades yet."

    lines = []
    for t in closed_trades:
        gain = t["realized_gain"]
        sign = "+" if gain >= 0 else ""
        lines.append(
            f"  {t['symbol']} | sold {t['sell_date']} | "
            f"P&L: {sign}${gain:,.2f} ({sign}{t['realized_gain_pct']:.1f}%) | "
            f"{t['tax_category'] or 'N/A'}"
        )
        if t["sell_reasoning"]:
            lines.append(f"    Sell thesis: {t['sell_reasoning'][:200]}")

    return "\n".join(lines)


async def update_playbook(db: AsyncSession, today: date) -> Playbook:
    """
    Read recent closed trade outcomes, ask Claude to update the playbook,
    and persist the new version. Returns the updated playbook.
    """
    playbook = await get_playbook(db)
    closed_trades = await _get_recent_closed_trades(db)

    if not closed_trades:
        logger.info("No closed trades to learn from — skipping playbook update")
        return playbook

    closed_trades_text = _format_closed_trades_for_prompt(closed_trades)

    user_content = f"""Today's date: {today}

## Current Playbook
{playbook.content}

## Recent Closed Trades (most recent first)
{closed_trades_text}

Review these outcomes against the playbook and produce an updated version that reflects what we've learned."""

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        system=UPDATE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    updated_content = response.content[0].text.strip()

    playbook.content = updated_content
    playbook.version += 1
    await db.commit()
    await db.refresh(playbook)

    logger.info("Playbook updated to version %d", playbook.version)
    return playbook
