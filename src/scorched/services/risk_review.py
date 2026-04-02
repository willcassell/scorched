"""Call 3: Risk committee adversarial review of trade recommendations."""
import json
import logging
import re

from ..prompts import load_prompt

logger = logging.getLogger(__name__)

RISK_REVIEW_SYSTEM = load_prompt("risk_review")


def build_risk_review_prompt(
    recommendations: list[dict],
    portfolio: dict,
    analysis_text: str,
    playbook_excerpt: str,
) -> str:
    """Build the user prompt for the risk committee review call."""
    lines = ["## Proposed Trades for Review\n"]
    for rec in recommendations:
        lines.append(
            f"- **{rec.get('action', '').upper()} {rec.get('symbol', '')}**: "
            f"qty {rec.get('quantity', '?')}, "
            f"confidence {rec.get('confidence', '?')}\n"
            f"  Reasoning: {rec.get('reasoning', 'N/A')}\n"
            f"  Key risks: {rec.get('key_risks', 'N/A')}"
        )

    lines.append("\n## Current Portfolio")
    lines.append(f"Cash: ${portfolio.get('cash_balance', 0):,.2f}")
    for pos in portfolio.get("positions", []):
        lines.append(
            f"  {pos.get('symbol', '?')}: {pos.get('shares', 0)} shares, "
            f"{pos.get('days_held', '?')}d held, "
            f"unrealized {'+' if pos.get('unrealized_gain', 0) >= 0 else ''}"
            f"${pos.get('unrealized_gain', 0):,.2f}"
        )

    if analysis_text:
        truncated_analysis = analysis_text[:3000]
        if len(analysis_text) > 3000:
            truncated_analysis += "..."
        lines.append(f"\n## Today's Analysis Summary\n{truncated_analysis}")

    if playbook_excerpt:
        truncated_playbook = playbook_excerpt[:1500]
        if len(playbook_excerpt) > 1500:
            truncated_playbook += "..."
        lines.append(f"\n## Recent Playbook Learnings\n{truncated_playbook}")

    return "\n".join(lines)


def parse_risk_review_response(raw: str) -> list[dict]:
    """Parse the risk review JSON response. Returns list of decision dicts, or [] on failure."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Try extracting from markdown code fences
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
            except json.JSONDecodeError:
                logger.warning("Failed to parse risk review response (even after fence extraction)")
                return []
        else:
            logger.warning("Failed to parse risk review response as JSON")
            return []

    return parsed.get("decisions", [])
