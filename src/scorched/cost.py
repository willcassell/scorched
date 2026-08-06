"""Token cost estimation. Prices in USD per million tokens as of 2026-02."""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import TokenUsage

# Maximum daily Claude spend before the system refuses new calls.
# Configurable via DAILY_COST_CEILING_USD env var (not yet wired —
# hardcoded default is generous enough for normal operation).
DAILY_COST_CEILING_USD = Decimal("5.00")

# (input_usd_per_mtok, output_usd_per_mtok, thinking_usd_per_mtok)
#
# Thinking tokens ARE output tokens — the API bills them at the output rate and
# exposes no separate thinking price (there is no `thinking_tokens` usage field
# either; our split is estimated from `output_tokens`). The third element must
# therefore equal the second. It was set to the INPUT rate until 2026-08-05,
# which under-reported every call that recorded a nonzero thinking split.
_PRICING: dict[str, tuple[float, float, float]] = {
    "claude-sonnet-4-5":          (3.0,  15.0,  15.0),
    "claude-sonnet-4-6":          (3.0,  15.0,  15.0),
    "claude-sonnet-5":            (3.0,  15.0,  15.0),
    "claude-opus-4-6":            (5.0,  25.0,  25.0),
    "claude-opus-4-8":            (5.0,  25.0,  25.0),
    "claude-haiku-4-5-20251001":  (1.0,  5.0,   5.0),
    "claude-haiku-4-5":           (1.0,  5.0,   5.0),
    "claude-opus-5":              (5.0,  25.0,  25.0),
    "claude-fable-5":             (10.0, 50.0,  50.0),
}
_DEFAULT_PRICING = (5.0, 25.0, 25.0)


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int = 0,
) -> Decimal:
    in_rate, out_rate, think_rate = _PRICING.get(model, _DEFAULT_PRICING)
    cost = (
        input_tokens * in_rate / 1_000_000
        + output_tokens * out_rate / 1_000_000
        + thinking_tokens * think_rate / 1_000_000
    )
    return Decimal(str(round(cost, 6)))


async def record_usage(
    db: AsyncSession,
    session_id: int | None,
    call_type: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int = 0,
) -> TokenUsage:
    cost = estimate_cost(model, input_tokens, output_tokens, thinking_tokens)
    row = TokenUsage(
        session_id=session_id,
        call_type=call_type,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        estimated_cost_usd=cost,
    )
    db.add(row)
    return row


async def get_today_cost(db: AsyncSession) -> Decimal:
    """Sum today's Claude API cost from token_usage table."""
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None,
    )
    result = await db.execute(
        select(func.coalesce(func.sum(TokenUsage.estimated_cost_usd), 0))
        .where(TokenUsage.created_at >= today_start)
    )
    return Decimal(str(result.scalar()))


async def check_daily_cost_ceiling(db: AsyncSession) -> None:
    """Raise if today's Claude spend exceeds the daily ceiling.

    Call before each Claude API call to prevent runaway costs.
    """
    today_cost = await get_today_cost(db)
    if today_cost >= DAILY_COST_CEILING_USD:
        raise RuntimeError(
            f"Daily Claude cost ceiling exceeded: ${today_cost:.4f} >= "
            f"${DAILY_COST_CEILING_USD} — refusing new API calls"
        )
