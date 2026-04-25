"""Intraday monitoring endpoint — evaluates triggered positions via Claude."""
import asyncio
import json
import logging
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from .deps import require_owner_pin

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
from ..tz import market_today

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/intraday", tags=["intraday"])

STRATEGY_PATH = Path(__file__).resolve().parent.parent.parent.parent / "strategy.json"


def _compute_emergency_sell_limit(
    current_price: Decimal,
    buffer_pct: Decimal,
    floor_usd: Decimal = Decimal("0.05"),
) -> Decimal:
    """Return a marketable limit price below current.

    Effective buffer = max(current * buffer_pct/100, floor_usd). Floor protects
    low-priced names where 1% rounds to one tick.
    """
    pct_buffer = Decimal(str(current_price)) * Decimal(str(buffer_pct)) / Decimal("100")
    effective_buffer = max(pct_buffer, Decimal(str(floor_usd)))
    return (Decimal(str(current_price)) - effective_buffer).quantize(Decimal("0.01"))


def _load_emergency_buffer_pct() -> Decimal:
    """Read intraday_monitor.emergency_sell_buffer_pct from strategy.json (default 1.0)."""
    try:
        with open(STRATEGY_PATH) as f:
            data = json.load(f)
        return Decimal(str(data.get("intraday_monitor", {}).get("emergency_sell_buffer_pct", 1.0)))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return Decimal("1.0")


def _load_emergency_buffer_floor_usd() -> Decimal:
    """Read intraday_monitor.emergency_sell_buffer_floor_usd from strategy.json (default 0.05)."""
    try:
        with open(STRATEGY_PATH) as f:
            data = json.load(f)
        return Decimal(str(data.get("intraday_monitor", {}).get("emergency_sell_buffer_floor_usd", 0.05)))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return Decimal("0.05")


def _load_hard_stop_pct() -> float:
    """Read hard stop threshold from strategy.json (default 8.0%).

    This is the deterministic auto-exit threshold (rule #4: -8% from entry).
    It is intentionally separate from `position_drop_from_entry_pct`, which
    is the looser Claude-evaluation trigger (default 5%).
    """
    try:
        with open(STRATEGY_PATH) as f:
            data = json.load(f)
        return float(data.get("intraday_monitor", {}).get("hard_stop_pct", 8.0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return 8.0


def _is_hard_stop(trigger: IntradayTriggerItem, hard_stop_pct: float) -> tuple[bool, float]:
    """Check if position has hit the hard stop threshold.

    Returns (is_hard_stop, drop_pct).
    """
    if trigger.entry_price <= 0:
        return False, 0.0
    drop_pct = float((trigger.entry_price - trigger.current_price) / trigger.entry_price * 100)
    return drop_pct >= hard_stop_pct, drop_pct


def _fresh_price(symbol: str) -> Decimal | None:
    """Fetch a fresh snapshot price from Alpaca (sync, runs in executor)."""
    try:
        from ..services.alpaca_data import fetch_snapshots_sync
        snaps = fetch_snapshots_sync([symbol])
        if symbol in snaps:
            return Decimal(str(snaps[symbol]["current_price"]))
    except Exception as exc:
        logger.warning("Fresh snapshot fetch failed for %s: %s", symbol, exc)
    return None


async def _execute_sell(
    trigger: IntradayTriggerItem,
    sell_qty: Decimal,
    db: AsyncSession,
    use_emergency_limit: bool = False,
) -> tuple[dict | None, str | None]:
    """Execute a sell via broker. Returns (trade_result, error_msg).

    When use_emergency_limit=True, the limit price is set below current
    (current * (1 - emergency_buffer_pct/100)) so the order fills in fast
    falling markets. Used for hard-stop exits.
    """
    broker = get_broker(db)

    # Fetch fresh price from Alpaca — don't use stale trigger.current_price (#16)
    loop = asyncio.get_running_loop()
    fresh = await loop.run_in_executor(None, _fresh_price, trigger.symbol)
    base = fresh or Decimal(str(trigger.current_price))
    if use_emergency_limit:
        limit_price = _compute_emergency_sell_limit(
            base,
            _load_emergency_buffer_pct(),
            _load_emergency_buffer_floor_usd(),
        )
    else:
        limit_price = base.quantize(Decimal("0.01"))

    # Deterministic idempotency key for intraday sells (#8)
    today = market_today().isoformat()
    client_oid = f"scorched-intraday-{trigger.symbol}-{today}"

    try:
        result = await broker.submit_sell(
            symbol=trigger.symbol,
            qty=sell_qty,
            limit_price=limit_price,
            recommendation_id=None,
            _client_order_id_override=client_oid,
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
        elif result["status"] == "submitted":
            # Fire-and-forget Alpaca order — treat as success, reconcile later
            trade_result = {
                "trade_id": None,
                "shares": float(sell_qty),
                "execution_price": float(limit_price),
                "realized_gain": None,
            }
            logger.info(
                "Intraday exit submitted: SELL %s %s shares @ limit %s (reconcile later)",
                trigger.symbol, sell_qty, limit_price,
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


@router.post("/evaluate", response_model=IntradayEvaluateResponse, dependencies=[Depends(require_owner_pin)])
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
            trade_result, err = await _execute_sell(trigger, trigger.shares, db, use_emergency_limit=True)
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
        # Check daily cost ceiling before calling Claude
        from ..cost import check_daily_cost_ceiling
        try:
            await check_daily_cost_ceiling(db)
        except RuntimeError as e:
            logger.warning("Skipping Claude eval for %s: %s", trigger.symbol, e)
            decisions.append(IntradayDecision(
                symbol=trigger.symbol,
                action="hold",
                reasoning=f"Daily cost ceiling exceeded — holding without evaluation",
                trade_result=None,
            ))
            continue

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

    # Run reconciliation after any sells executed
    any_sells = any(d.trade_result is not None for d in decisions)
    if any_sells:
        try:
            from ..services.reconciliation import check_reconciliation
            from ..services.telegram import send_telegram as tg_send
            recon = await check_reconciliation(db)
            if recon["has_mismatches"]:
                lines = ["TRADEBOT // RECON WARNING (intraday)"]
                for m in recon["mismatches"]:
                    lines.append(f"  {m['symbol']}: local={m['local_qty']}, broker={m['broker_qty']}")
                await tg_send("\n".join(lines))
                logger.warning("Reconciliation mismatch after intraday sell: %s", recon["mismatches"])
        except Exception:
            logger.warning("Post-sell reconciliation check failed", exc_info=True)

    return IntradayEvaluateResponse(decisions=decisions)
