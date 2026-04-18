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
    correlation_warnings: list[str] | None = None,
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

    if correlation_warnings:
        lines.append("\n## CORRELATION WARNINGS")
        lines.append(
            "The following buy candidates show high 20-day return correlation "
            "with existing held positions. Holding highly correlated positions "
            "concentrates risk — they behave as a single bet with amplified exposure."
        )
        for warning in correlation_warnings:
            lines.append(f"- {warning}")

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


def parse_risk_review_response(raw: str) -> list[dict] | None:
    """Parse the risk review JSON response.

    Returns list of decision dicts on success, or **None** on parse failure.
    Callers must treat None as fail-closed (reject all buys).
    """
    from .claude_client import parse_json_response, validate_llm_output, RiskReviewOutput

    parsed = parse_json_response(raw)
    if not parsed:
        logger.warning("Failed to parse risk review response as JSON")
        return None

    validated = validate_llm_output(parsed, RiskReviewOutput)
    if validated:
        return [d.model_dump() for d in validated.decisions]
    return parsed.get("decisions", [])
