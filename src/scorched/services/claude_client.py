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

class CandidateEntry(BaseModel):
    symbol: str
    conviction: str = "medium"  # high | medium | low
    catalyst: str = ""
    entry_rationale: str = ""
    key_risks: str = ""

    @field_validator("symbol", mode="before")
    @classmethod
    def uppercase_symbol(cls, v: str) -> str:
        return v.upper()

    @field_validator("conviction", mode="before")
    @classmethod
    def lowercase_conviction(cls, v: str) -> str:
        return (v or "medium").lower()


class PositionAction(BaseModel):
    symbol: str
    action: str  # hold | exit | trim | monitor
    rule: str = "none"
    reasoning: str = ""

    @field_validator("symbol", mode="before")
    @classmethod
    def uppercase_symbol(cls, v: str) -> str:
        return v.upper()

    @field_validator("action", mode="before")
    @classmethod
    def lowercase_action(cls, v: str) -> str:
        return v.lower()


class AnalysisOutput(BaseModel):
    analysis: str
    candidates: list[CandidateEntry] = []
    position_actions: list[PositionAction] = []

    @field_validator("candidates", mode="before")
    @classmethod
    def normalize_candidates(cls, v):
        """Accept both new structured format and legacy list-of-strings."""
        if not v:
            return []
        if isinstance(v[0], str):
            return [{"symbol": s, "conviction": "medium"} for s in v][:5]
        return v[:5]


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
    action: str  # "buy" or "sell" — needed so recommender can filter rejected buys
    verdict: str
    reason: str

    @field_validator("symbol", mode="before")
    @classmethod
    def uppercase_symbol(cls, v: str) -> str:
        return v.upper()

    @field_validator("action", mode="before")
    @classmethod
    def lowercase_action(cls, v: str) -> str:
        return (v or "").lower()

    @field_validator("verdict", mode="before")
    @classmethod
    def lowercase_verdict(cls, v: str) -> str:
        return (v or "").lower()


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
    """Parse JSON from a response, handling markdown code fences and trailing text.

    Strategies tried in order:
    1. Direct json.loads (clean JSON)
    2. raw_decode — parses the first JSON object, ignoring trailing text
    3. Extract from markdown code fence, then raw_decode on that
    4. Find first '{' and raw_decode from there
    """
    # Strategy 1: clean JSON
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strategy 2: raw_decode from start (handles trailing text after JSON)
    decoder = json.JSONDecoder()
    stripped = raw.lstrip()
    if stripped.startswith("{"):
        try:
            obj, _ = decoder.raw_decode(stripped)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Strategy 3: extract from markdown code fence
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        try:
            obj, _ = decoder.raw_decode(match.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            # Try greedy match as fallback (multiple objects less likely inside fence)
            match2 = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
            if match2:
                try:
                    return json.loads(match2.group(1))
                except json.JSONDecodeError:
                    pass

    # Strategy 4: find first '{' anywhere and raw_decode
    brace_pos = raw.find("{")
    if brace_pos >= 0:
        try:
            obj, _ = decoder.raw_decode(raw[brace_pos:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Strategy 5: brace-match from first '{' to its matching '}' (handles
    # trailing prose that breaks raw_decode, e.g. unbalanced quotes in commentary)
    if brace_pos >= 0:
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(raw[brace_pos:], start=brace_pos):
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = raw[brace_pos:i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break

    logger.warning(
        "parse_json_response: could not extract JSON from response (%d chars). "
        "First 500 chars: %s",
        len(raw), raw[:500],
    )
    return {}


# ── Internal helpers ─────────────────────────────────────────────────────────

# Per-request Claude timeout. Our longest call (analysis w/ 16k thinking) has
# historically landed under 200s; give 5x headroom before giving up. This is
# enforced at the httpx layer; retry.py catches APITimeoutError and retries.
_CLAUDE_TIMEOUT_S = 300.0

_JSON_FIXUP_PROMPT = (
    "Your previous response could not be parsed as valid JSON. "
    "Please respond with ONLY the JSON object — no commentary, no markdown "
    "fences, no explanation before or after. Just the raw JSON starting with { "
    "and ending with }."
)


def _client() -> anthropic.AsyncAnthropic:
    """Return an AsyncAnthropic client with explicit timeout.

    Async avoids blocking the FastAPI event loop during long LLM calls, so
    the dashboard and other endpoints remain responsive while Claude thinks.
    """
    return anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=_CLAUDE_TIMEOUT_S,
    )


# ── Call wrappers ────────────────────────────────────────────────────────────

async def call_analysis(strategy: str, guidance: str, user_content: str, tracker=None):
    """Call 1: Analysis with extended thinking.

    Returns (response, analysis_text, thinking_text, candidates, position_actions)
    where candidates is list[CandidateEntry] and position_actions is list[PositionAction].
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

    # Retry once on parse failure — cheap fix-up call (no extended thinking)
    if not parsed:
        logger.warning(
            "Call 1 JSON parse failed (%d chars) — retrying with fix-up prompt",
            len(analysis_raw),
        )
        retry_response = await claude_call_with_retry(
            _client(), "Call 1 (analysis JSON fix)",
            model=MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": analysis_raw},
                {"role": "user", "content": _JSON_FIXUP_PROMPT},
            ],
        )
        analysis_raw = retry_response.content[0].text
        parsed = parse_json_response(analysis_raw)
        logger.info("Call 1 JSON fix-up %s", "succeeded" if parsed else "also failed")

    validated = validate_llm_output(parsed, AnalysisOutput) if parsed else None
    if validated:
        analysis_text = validated.analysis
        candidates = validated.candidates
        position_actions = validated.position_actions
    else:
        # Fallback: preserve raw analysis text; no structured candidates/actions
        analysis_text = parsed.get("analysis", analysis_raw)
        candidates = []
        position_actions = []

    return response, analysis_text, thinking_text, candidates, position_actions


async def call_decision(
    strategy: str,
    guidance: str,
    playbook_content: str,
    min_cash_pct: int,
    user_content: str,
    *,
    max_position_pct: int = 20,
    max_holdings: int = 5,
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
        max_position_pct=max_position_pct,
        max_holdings=max_holdings,
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

    # Retry once on parse failure — sends back the bad response and asks for clean JSON
    if not parsed:
        logger.warning(
            "Call 2 JSON parse failed (%d chars) — retrying with fix-up prompt",
            len(decision_raw),
        )
        ctx2 = track_call(tracker, "claude", "decision_retry") if tracker else nullcontext()
        with ctx2:
            retry_response = await claude_call_with_retry(
                _client(), "Call 2 (decision JSON fix)",
                model=MODEL,
                max_tokens=2048,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": decision_raw},
                    {"role": "user", "content": _JSON_FIXUP_PROMPT},
                ],
            )
        decision_raw = retry_response.content[0].text
        parsed = parse_json_response(decision_raw)
        logger.info("Call 2 JSON fix-up %s", "succeeded" if parsed else "also failed")

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
            max_tokens=2048,
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


async def call_weekly_reflection(user_content: str):
    """Weekly reflection — reviews past trades for learnings. Uses sonnet.

    Returns (response, raw_text).
    """
    system_prompt = load_prompt("weekly_reflection")
    response = await claude_call_with_retry(
        _client(), "Weekly reflection",
        model=MODEL,
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    return response, response.content[0].text
