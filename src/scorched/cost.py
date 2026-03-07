"""Token cost estimation. Prices in USD per million tokens as of 2026-02."""
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from .models import TokenUsage

# (input_usd_per_mtok, output_usd_per_mtok, thinking_usd_per_mtok)
_PRICING: dict[str, tuple[float, float, float]] = {
    "claude-sonnet-4-5":          (3.0,  15.0,  3.0),
    "claude-sonnet-4-6":          (3.0,  15.0,  3.0),
    "claude-opus-4-6":            (15.0, 75.0,  15.0),
    "claude-haiku-4-5-20251001":  (0.8,  4.0,   0.8),
}
_DEFAULT_PRICING = (3.0, 15.0, 3.0)


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
