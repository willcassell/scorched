"""Call 4: EOD position management — review each open position and suggest stop adjustments.

Runs after the existing EOD review. Evaluates each position against today's
price action and recommends whether to tighten stops, take partial profit, or hold.
"""
import logging

from ..prompts import load_prompt

logger = logging.getLogger(__name__)

POSITION_MGMT_SYSTEM = load_prompt("position_mgmt")


def build_position_review_prompt(positions: list[dict], market_summary: str) -> str:
    """Build user prompt for position management review."""
    lines = ["## Open Positions for Review\n"]
    for pos in positions:
        lines.append(
            f"- {pos['symbol']}: {pos.get('shares', 0)} shares, "
            f"avg cost ${pos.get('avg_cost_basis', 0):.2f}, "
            f"current ${pos.get('current_price', 0):.2f}, "
            f"P&L {pos.get('unrealized_gain_pct', 0):+.1f}%, "
            f"held {pos.get('days_held', 0)} days"
        )

    lines.append(f"\n## Today's Market Summary\n{market_summary[:500]}")
    return "\n".join(lines)
