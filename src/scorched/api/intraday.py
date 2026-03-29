"""Intraday monitoring endpoint — evaluates triggered positions via Claude."""
import logging
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..broker import get_broker
from ..cost import record_usage
from ..database import get_db
from ..schemas import (
    IntradayDecision,
    IntradayEvaluateRequest,
    IntradayEvaluateResponse,
)
from ..services.claude_client import call_intraday_exit, parse_json_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/intraday", tags=["intraday"])


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

    for trigger in body.triggers:
        prompt = _build_exit_prompt(trigger, body.market_context)

        response, raw_text = call_intraday_exit(prompt)

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
            except Exception as e:
                logger.error("Intraday sell failed for %s: %s", trigger.symbol, e)
                reasoning += f" [SELL FAILED: {e}]"
                action = "hold"

        decisions.append(IntradayDecision(
            symbol=trigger.symbol,
            action=action,
            reasoning=reasoning,
            trade_result=trade_result,
        ))

    await db.commit()
    return IntradayEvaluateResponse(decisions=decisions)
