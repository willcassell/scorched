"""Call 4: EOD position management — review each open position and suggest stop adjustments.

Runs after the existing EOD review. Evaluates each position against today's
price action and recommends whether to tighten stops, take partial profit, or hold.
"""
import logging

logger = logging.getLogger(__name__)

POSITION_MGMT_SYSTEM = """You are reviewing open positions after market close. For each position, evaluate today's price action and recommend an action for tomorrow.

For each position, consider:
- How many days has it been held vs. the strategy's target holding period?
- Is the position approaching a stop loss or profit target?
- Did today's price action strengthen or weaken the original thesis?
- Are there any earnings or events approaching that create risk?

## Output format
Respond with valid JSON only:
{{
  "position_reviews": [
    {{
      "symbol": "TICKER",
      "action": "hold" or "tighten_stop" or "take_partial" or "exit_tomorrow",
      "new_stop_pct": null or float (e.g. -3.0 means set stop at -3% from current price),
      "reasoning": "1-2 sentences"
    }}
  ]
}}

Be conservative. "hold" is the default. Only recommend changes when today's action provides clear evidence."""


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
