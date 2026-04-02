"""Consolidated Claude API client — wraps all Anthropic SDK interactions.

Each ``call_*()`` function loads its prompt, creates a client, makes the API
call (with retry where appropriate), and returns the response plus extracted
data.  Callers remain responsible for DB writes and cost recording.
"""
import json
import logging
import re
from contextlib import nullcontext
from typing import Optional

import anthropic
from pydantic import BaseModel, field_validator, ValidationError

from ..api_tracker import track_call
from ..config import settings
from ..prompts import load_prompt
from ..retry import claude_call_with_retry

logger = logging.getLogger(__name__)


# ── Pydantic validation models for Claude outputs ──────────────────────────

class AnalysisOutput(BaseModel):
    analysis: str
    candidates: list[str]

    @field_validator("candidates", mode="before")
    @classmethod
    def normalise_candidates(cls, v: list) -> list[str]:
        return [s.upper() for s in v][:5]


class RecommendationEntry(BaseModel):
    symbol: str
    action: str
    suggested_price: float
    quantity: int
    reasoning: str
    confidence: str
    key_risks: str

    @field_validator("symbol", mode="before")
    @classmethod
    def uppercase_symbol(cls, v: str) -> str:
        return v.upper()

    @field_validator("action", mode="before")
    @classmethod
    def lowercase_action(cls, v: str) -> str:
        return v.lower()

    @field_validator("confidence", mode="before")
    @classmethod
    def lowercase_confidence(cls, v: str) -> str:
        return v.lower()


class DecisionOutput(BaseModel):
    research_summary: str
    recommendations: list[RecommendationEntry]


class RiskDecisionEntry(BaseModel):
    symbol: str
    verdict: str
    reason: str

    @field_validator("symbol", mode="before")
    @classmethod
    def uppercase_symbol(cls, v: str) -> str:
        return v.upper()

    @field_validator("verdict", mode="before")
    @classmethod
    def lowercase_verdict(cls, v: str) -> str:
        return v.lower()


class RiskReviewOutput(BaseModel):
    decisions: list[RiskDecisionEntry]


def validate_llm_output(raw_dict: dict, model_class: type[BaseModel]) -> Optional[BaseModel]:
    """Validate a parsed dict against a Pydantic model. Returns None on failure."""
    try:
        return model_class.model_validate(raw_dict)
    except ValidationError as e:
        logger.warning("LLM output validation failed for %s: %s", model_class.__name__, e)
        return None

# ── Shared constants ─────────────────────────────────────────────────────────
MODEL = "claude-sonnet-4-6"
THINKING_BUDGET = 16000  # tokens; ~$0.048/day (Tier 2 upgrade from 8K)


# ── Response helpers ─────────────────────────────────────────────────────────

def extract_text(content: list) -> str:
    """Extract the text block from a response that may contain thinking blocks."""
    for block in content:
        if block.type == "text":
            return block.text
    return ""


def extract_thinking(content: list) -> str:
    """Extract the thinking block text if present."""
    for block in content:
        if block.type == "thinking":
            return block.thinking
    return ""


def parse_json_response(raw: str) -> dict:
    """Parse JSON from a response, handling markdown code fences."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        return {}


# ── Internal helpers ─────────────────────────────────────────────────────────

def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


# ── Call wrappers ────────────────────────────────────────────────────────────

async def call_analysis(strategy: str, guidance: str, user_content: str, tracker=None):
    """Call 1: Analysis with extended thinking.

    Returns (response, analysis_text, thinking_text, candidates).
    """
    system_prompt = load_prompt("analysis").format(strategy=strategy, guidance=guidance)
    ctx = track_call(tracker, "claude", "analysis") if tracker else nullcontext()
    with ctx:
        response = await claude_call_with_retry(
            _client(), "Call 1 (analysis)",
            model=MODEL,
            max_tokens=THINKING_BUDGET + 2048,
            thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET},
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )

    analysis_raw = extract_text(response.content)
    thinking_text = extract_thinking(response.content)
    parsed = parse_json_response(analysis_raw)

    validated = validate_llm_output(parsed, AnalysisOutput) if parsed else None
    if validated:
        analysis_text = validated.analysis
        candidates = validated.candidates
    else:
        analysis_text = parsed.get("analysis", analysis_raw)
        candidates = [s.upper() for s in parsed.get("candidates", [])][:5]

    return response, analysis_text, thinking_text, candidates


async def call_decision(
    strategy: str,
    guidance: str,
    playbook_content: str,
    min_cash_pct: int,
    user_content: str,
    tracker=None,
):
    """Call 2: Decision (standard, no extended thinking).

    Returns (response, decision_raw_text, parsed_dict).
    """
    system_prompt = load_prompt("decision").format(
        min_cash_pct=min_cash_pct,
        playbook=playbook_content,
        strategy=strategy,
        guidance=guidance,
    )
    ctx = track_call(tracker, "claude", "decision") if tracker else nullcontext()
    with ctx:
        response = await claude_call_with_retry(
            _client(), "Call 2 (decision)",
            model=MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )

    decision_raw = response.content[0].text
    parsed = parse_json_response(decision_raw)
    if not parsed:
        parsed = {"research_summary": decision_raw, "recommendations": []}
    else:
        validated = validate_llm_output(parsed, DecisionOutput)
        if validated:
            parsed = validated.model_dump()

    return response, decision_raw, parsed


async def call_risk_review(user_content: str, tracker=None):
    """Call 3: Risk committee review.

    Returns (response, raw_text).
    """
    system_prompt = load_prompt("risk_review")
    ctx = track_call(tracker, "claude", "risk_review") if tracker else nullcontext()
    with ctx:
        response = await claude_call_with_retry(
            _client(), "Call 3 (risk review)",
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )

    return response, response.content[0].text


async def call_position_review(user_content: str):
    """Call 4: Position management review.

    Returns (response, raw_text).
    """
    system_prompt = load_prompt("position_mgmt")
    response = await claude_call_with_retry(
        _client(), "Call 4 (position review)",
        model=MODEL,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    return response, response.content[0].text


HAIKU_MODEL = "claude-haiku-4-5-20251001"


async def call_eod_review(user_content: str):
    """EOD review: distill learnings and update the playbook.

    Returns (response, updated_text).
    """
    system_prompt = load_prompt("eod_review")
    response = await claude_call_with_retry(
        _client(), "EOD review",
        model=HAIKU_MODEL,
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    return response, response.content[0].text.strip()


async def call_playbook_update(user_content: str):
    """Playbook update (uses claude-opus-4-6, not sonnet).

    Returns (response, updated_text).
    Raises anthropic.APIStatusError on failure after retries.
    """
    system_prompt = load_prompt("playbook_update")
    response = await claude_call_with_retry(
        _client(), "Playbook update",
        model=MODEL,
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    return response, response.content[0].text.strip()


async def call_intraday_exit(user_content: str):
    """Intraday exit evaluation — small focused call.

    Returns (response, raw_text).
    """
    system_prompt = load_prompt("intraday_exit")
    logger.info("Intraday exit evaluation call")
    response = await claude_call_with_retry(
        _client(), "Intraday exit",
        model=HAIKU_MODEL,
        max_tokens=512,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    return response, response.content[0].text
