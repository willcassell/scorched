"""Call 3: Risk committee adversarial review of trade recommendations."""
import json
import logging
import re

logger = logging.getLogger(__name__)

RISK_REVIEW_SYSTEM = """You are a skeptical risk committee reviewing proposed trades. Your default stance is REJECT unless the trade clearly passes ALL of the following checks:

1. **Thesis quality** — Is the reasoning specific, with a named catalyst and clear time horizon? Vague "looks good" reasoning = reject.
2. **Concentration risk** — Would this trade create excessive exposure to one sector or correlated positions?
3. **Timing risk** — Is there an earnings report, Fed meeting, or other binary event within the holding period that could invalidate the thesis?
4. **Loss pattern matching** — Does this trade resemble past losing patterns (chasing momentum after extended runs, buying into resistance, averaging down)?
5. **Risk/reward** — Is the upside at least 2x the downside? If the stop-loss distance implies more risk than the target gain, reject.
6. **Macro alignment** — Does the trade align with the current macro environment, or is it fighting the trend?

For SELL recommendations: approve them unless the reasoning is clearly wrong (e.g., selling at a loss when the thesis is still intact and no stop-loss was hit). Sells should almost always be approved — taking profits or cutting losses is rarely wrong.

Output valid JSON only:
{
  "review_summary": "1-2 sentence overall assessment of today's proposed trades",
  "decisions": [
    {
      "symbol": "TICKER",
      "action": "buy" or "sell",
      "verdict": "approve" or "reject",
      "reason": "Specific reason for the verdict (1-2 sentences)"
    }
  ]
}"""


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
        truncated_analysis = analysis_text[:800]
        if len(analysis_text) > 800:
            truncated_analysis += "..."
        lines.append(f"\n## Today's Analysis Summary\n{truncated_analysis}")

    if playbook_excerpt:
        truncated_playbook = playbook_excerpt[:500]
        if len(playbook_excerpt) > 500:
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
