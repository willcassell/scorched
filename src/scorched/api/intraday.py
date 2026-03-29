"""Intraday monitoring endpoint — evaluates triggered positions via Claude."""
import json
import logging
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..broker import get_broker
from ..cost import record_usage
from ..database import get_db
from ..schemas import (
    IntradayDecision,
    IntradayEvaluateRequest,
    IntradayEvaluateResponse,
    IntradayTriggerItem,
)
from ..services.claude_client import call_intraday_exit, parse_json_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/intraday", tags=["intraday"])

STRATEGY_PATH = Path(__file__).resolve().parent.parent.parent.parent / "strategy.json"


def _load_hard_stop_pct() -> float:
    """Read hard stop threshold from strategy.json (default 5.0%)."""
    try:
        with open(STRATEGY_PATH) as f:
            data = json.load(f)
        return float(data.get("intraday_monitor", {}).get("position_drop_from_entry_pct", 5.0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return 5.0


def _is_hard_stop(trigger: IntradayTriggerItem, hard_stop_pct: float) -> tuple[bool, float]:
    """Check if position has hit the hard stop threshold.

    Returns (is_hard_stop, drop_pct).
    """
    if trigger.entry_price <= 0:
        return False, 0.0
    drop_pct = float((trigger.entry_price - trigger.current_price) / trigger.entry_price * 100)
    return drop_pct >= hard_stop_pct, drop_pct


async def _execute_sell(
    trigger: IntradayTriggerItem,
    sell_qty: Decimal,
    db: AsyncSession,
) -> tuple[dict | None, str | None]:
    """Execute a sell via broker. Returns (trade_result, error_msg)."""
    broker = get_broker(db)
    try:
        result = await broker.submit_sell(
            symbol=trigger.symbol,
            qty=sell_qty,
            limit_price=trigger.current_price,
            recommendation_id=None,
        )
        if result["status"] == "filled":
            trade_result = {
                "trade_id": result.get("trade_id"),
                "shares": float(sell_qty),
                "execution_price": float(result["filled_avg_price"]),
                "realized_gain": float(result.get("realized_gain") or 0),
            }
            logger.info(
                "Intraday exit executed: SELL %s %s shares @ %s",
                trigger.symbol, sell_qty, result["filled_avg_price"],
            )
            return trade_result, None
        else:
            error = f"Order not filled (status: {result['status']})"
            logger.warning("Intraday sell for %s: %s", trigger.symbol, error)
            return None, error
    except Exception as e:
        logger.error("Intraday sell failed for %s: %s", trigger.symbol, e)
        return None, str(e)


def _build_exit_prompt(trigger, market_ctx) -> str:
    """Build the user prompt for a single triggered position."""
    pct_change = float((trigger.current_price - trigger.entry_price) / trigger.entry_price * 100) if trigger.entry_price else 0
    lines = [
        f"Position: {trigger.symbol}, {trigger.shares} shares, "
        f"entry ${trigger.entry_price}, current ${trigger.current_price} "
        f"({pct_change:+.1f}%)",
        "Triggers fired:",
    ]
    for reason in trigger.trigger_reasons:
        lines.append(f"  - {reason}")
    lines.append(
        f"Today's action: Opened ${trigger.today_open}, "
        f"high ${trigger.today_high}, low ${trigger.today_low}"
    )
    lines.append(f"SPY today: {market_ctx.spy_change_pct:+.1f}%")
    lines.append(f"VIX: {market_ctx.vix_current:.1f}")
    lines.append(f"Days held: {trigger.days_held}")
    if trigger.original_reasoning:
        lines.append(f"Original thesis: {trigger.original_reasoning[:300]}")
    lines.append(
        "\nShould this position be exited? Respond with JSON: "
        '{"action": "exit_full"|"exit_partial"|"hold", "partial_pct": null or int, "reasoning": "..."}'
    )
    return "\n".join(lines)


@router.post("/evaluate", response_model=IntradayEvaluateResponse)
async def evaluate_triggers(
    body: IntradayEvaluateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Evaluate triggered positions via Claude and execute exits."""
    decisions = []
    hard_stop_pct = _load_hard_stop_pct()

    for trigger in body.triggers:
        # ── Hard stop check — bypass Claude entirely ──────────────────
        is_stop, drop_pct = _is_hard_stop(trigger, hard_stop_pct)
        if is_stop:
            logger.info("HARD STOP %s: down %.1f%% — auto-selling", trigger.symbol, drop_pct)
            reasoning = (
                f"Hard stop triggered: position down {drop_pct:.1f}% from entry "
                f"(>= {hard_stop_pct:.1f}% threshold). Auto-exit without Claude evaluation."
            )
            trade_result, err = await _execute_sell(trigger, trigger.shares, db)
            action = "exit_full"
            if err:
                reasoning += f" [SELL FAILED: {err}]"
                action = "hold"

            decisions.append(IntradayDecision(
                symbol=trigger.symbol,
                action=action,
                reasoning=reasoning,
                trade_result=trade_result,
            ))
            continue

        # ── Normal Claude evaluation path ─────────────────────────────
        prompt = _build_exit_prompt(trigger, body.market_context)

        response, raw_text = await call_intraday_exit(prompt)

        # Record usage
        usage = response.usage
        await record_usage(
            db,
            session_id=None,
            call_type="intraday_exit",
            model=response.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

        parsed = parse_json_response(raw_text)
        action = parsed.get("action", "hold")
        reasoning = parsed.get("reasoning", raw_text[:200])
        partial_pct = parsed.get("partial_pct")

        logger.info(
            "Intraday %s %s: %s — %s",
            trigger.symbol, action, reasoning[:100],
            trigger.trigger_reasons,
        )

        trade_result = None

        if action in ("exit_full", "exit_partial"):
            sell_qty = trigger.shares
            if action == "exit_partial" and partial_pct:
                sell_qty = (trigger.shares * Decimal(str(partial_pct)) / 100).quantize(Decimal("1"))
                sell_qty = max(sell_qty, Decimal("1"))

            trade_result, err = await _execute_sell(trigger, sell_qty, db)
            if err:
                reasoning += f" [SELL FAILED: {err}]"
                action = "hold"

        decisions.append(IntradayDecision(
            symbol=trigger.symbol,
            action=action,
            reasoning=reasoning,
            trade_result=trade_result,
        ))

    await db.commit()
    return IntradayEvaluateResponse(decisions=decisions)
