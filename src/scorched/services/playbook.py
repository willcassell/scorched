"""Playbook service: reads and updates the bot's living strategy document."""
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Playbook, TradeHistory, TradeRecommendation
from .claude_client import call_playbook_update as _call_playbook_update
from .strategy import load_analyst_guidance, load_strategy
from .telegram import send_telegram

logger = logging.getLogger(__name__)

INITIAL_PLAYBOOK = """# Trading Playbook

## Strategy Overview
No trades have been made yet. The declared strategy (from strategy.json + analyst_guidance.md) governs all rules — this playbook records observed outcomes only, not new rules.

## What Has Worked
(No data yet)

## What Has Not Worked
(No data yet)

## Sectors / Themes to Favor
(No data yet)

## Sectors / Themes to Avoid
(No data yet)

## Recurring Mistakes
(No data yet)

## Notes
This playbook records *learnings from outcomes*. Non-negotiable rules (holding period, stop loss, partial-sell thresholds, cash floor, concentration) live in strategy.json and analyst_guidance.md. The playbook must not install competing numeric rules.
"""


# Drift detection — these patterns catch the common ways a playbook update
# can smuggle in a competing rule system that contradicts strategy.json.
# If any fire, we refuse the update and keep the prior playbook.
_DRIFT_PATTERNS: list[tuple[str, str]] = [
    (r"\b10[-\s]?day (ceiling|hard|maximum|time stop|rule)\b", "10-day time ceiling"),
    (r"\bday\s*10\b[^.\n]{0,80}\b(exit|sell|close)\b", "hard day-10 exit rule"),
    (r"\b3[-\s]?(to[-\s]?)?10\s+(trading|calendar)?\s*days?\b", "3-10 day holding window"),
    (r"\btier\s*1\b[^.\n]{0,80}\b-?3\s*%", "-3% Tier 1 stop rule"),
    (r"\btier\s*2\b[^.\n]{0,80}\b-?5\s*%", "-5% Tier 2 stop rule"),
    (r"\bpartial[-\s]sell[^.\n]{0,40}\+?\s*8\s*%", "+8% partial-sell rule"),
    (r"\+8\s*%\s+gain\b[^.\n]{0,40}\b(sell|partial|trim)", "+8% partial-sell rule"),
    (r"\b7[-\s]?day flat\b", "7-day flat-position rule"),
]


def _check_playbook_drift(new_content: str) -> list[str]:
    """Return a list of detected drift patterns. Empty list = clean."""
    findings: list[str] = []
    for pattern, desc in _DRIFT_PATTERNS:
        if re.search(pattern, new_content, re.IGNORECASE):
            findings.append(desc)
    return findings


def _extract_hard_rules(analyst_guidance: str) -> str:
    """Pull the 'Hard Rules' section out of analyst_guidance.md for the prompt."""
    if not analyst_guidance:
        return ""
    # Grab from "## Hard Rules" to the next "---" or next top-level "##" section.
    m = re.search(
        r"(##\s*Hard Rules[^\n]*\n.*?)(?=\n---|\n##\s|\Z)",
        analyst_guidance,
        re.DOTALL | re.IGNORECASE,
    )
    return m.group(1).strip() if m else ""


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


_REJECTION_LOG_DIR = Path("/app/logs/playbook_rejections")


def _persist_rejected_playbook(
    rejected: str,
    current: str,
    prior_version: int,
    drift: list[str],
) -> None:
    """Write the rejected playbook payload to a timestamped file for forensics."""
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        _REJECTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = _REJECTION_LOG_DIR / f"{ts}_v{prior_version}.md"
        flagged = "\n".join(f"- {d}" for d in drift)
        path.write_text(
            f"# Playbook Rejection — {ts}\n\n"
            f"**Prior version kept:** v{prior_version}\n\n"
            f"**Drift patterns flagged:**\n{flagged}\n\n"
            f"---\n\n## Proposed (Rejected) Content\n\n```\n{rejected}\n```\n\n"
            f"---\n\n## Current (Kept) Content\n\n```\n{current}\n```\n",
            encoding="utf-8",
        )
        logger.info("Persisted rejected playbook to %s", path)
    except Exception as e:  # noqa: BLE001 — file logging is best-effort
        logger.warning("Failed to persist rejected playbook to disk: %s", e)


async def update_playbook(db: AsyncSession, today: date) -> Playbook:
    """
    Read recent closed trade outcomes, ask Claude to update the playbook,
    and persist the new version. Returns the updated playbook.

    The update prompt injects the declared strategy + analyst-guidance hard rules
    and marks them immutable. After Claude responds, a drift check scans the
    output for known rule-level contradictions (e.g., "10-day ceiling", "+8%
    partial sell"). If drift is detected, the update is rejected and a Telegram
    alert is sent — the prior playbook is preserved.
    """
    playbook = await get_playbook(db)
    closed_trades = await _get_recent_closed_trades(db)

    if not closed_trades:
        logger.info("No closed trades to learn from — skipping playbook update")
        return playbook

    closed_trades_text = _format_closed_trades_for_prompt(closed_trades)
    strategy_text = load_strategy()
    # Raw guidance + separately-rendered addendum: we slice out Hard Rules
    # from the raw text (the regex terminates on any "## ", so running it on
    # load_effective_guidance() would drop the addendum), then append the
    # addendum verbatim after the rules block.
    from .guidance import build_overrides_addendum
    hard_rules = _extract_hard_rules(load_analyst_guidance())
    overrides_addendum = build_overrides_addendum()

    strategy_block = (
        strategy_text
        + ("\n\n## Hard Rules (from analyst_guidance.md)\n" + hard_rules if hard_rules else "")
        + overrides_addendum
    )

    user_content = f"""Today's date: {today}

---

## DECLARED STRATEGY — IMMUTABLE
The following rules are set by the user in strategy.json and analyst_guidance.md.
You MUST NOT contradict them, replace them with tighter numbers, or install competing numeric rules (e.g., "10-day ceiling", "-3% Tier 1", "+8% partial sell", "3-10 day holding"). If recent trades suggest the strategy itself should change, add a "Suggested Strategy Changes (for human review)" section at the end — do not apply changes yourself.

{strategy_block}

---

## Current Playbook
{playbook.content}

---

## Recent Closed Trades (most recent first)
{closed_trades_text}

---

Review these outcomes against the playbook and produce an updated version that records what we learned about *specific trades, sectors, and behavioral patterns*. Do not restate or rewrite the declared strategy — reference it. Return ONLY the full updated playbook text."""

    try:
        response, updated_content = await _call_playbook_update(user_content)
    except (anthropic.APIStatusError, anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
        logger.error("Playbook update failed after all retries (%s) — using stale playbook", type(e).__name__)
        return playbook

    drift = _check_playbook_drift(updated_content)
    if drift:
        logger.error(
            "Playbook update REJECTED — drift detected: %s. Keeping prior playbook v%d.",
            ", ".join(drift), playbook.version,
        )
        _persist_rejected_playbook(updated_content, playbook.content, playbook.version, drift)
        try:
            await send_telegram(
                "⚠️ Playbook update rejected — strategy drift detected:\n"
                + "\n".join(f"  • {d}" for d in drift)
                + f"\n\nKept playbook v{playbook.version}. Review prompt or current playbook."
            )
        except Exception as e:  # noqa: BLE001 — Telegram is best-effort
            logger.warning("Failed to send drift-alert Telegram: %s", e)
        return playbook

    playbook.content = updated_content
    playbook.version += 1
    await db.commit()
    await db.refresh(playbook)

    logger.info("Playbook updated to version %d (no drift)", playbook.version)
    return playbook
