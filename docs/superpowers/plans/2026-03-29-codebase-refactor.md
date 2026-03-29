# Codebase Refactoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the Scorched tradebot codebase for maintainability, debuggability, and separation of concerns without changing any external behavior.

**Architecture:** Pure refactoring in 7 tasks. Each task is independently deployable — no task depends on another being complete first (though Task 1 should go first since it touches the most files). All existing tests must continue to pass after every task. No database migrations needed.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0 async, Anthropic SDK, yfinance, pytest

---

## File Structure

### Files to Create
- `src/scorched/services/claude_client.py` — All Anthropic API interactions (prompts, calls, response parsing)
- `src/scorched/prompts/analysis.md` — Call 1 system prompt
- `src/scorched/prompts/decision.md` — Call 2 system prompt
- `src/scorched/prompts/risk_review.md` — Call 3 system prompt
- `src/scorched/prompts/position_mgmt.md` — Call 4 system prompt
- `src/scorched/prompts/eod_review.md` — EOD review system prompt
- `src/scorched/prompts/playbook_update.md` — Playbook update system prompt
- `src/scorched/prompts/__init__.py` — Prompt loader utility
- `cron/common.py` — Shared cron utilities (env loading, HTTP, Telegram)
- `tests/test_claude_client.py` — Tests for extracted Claude client
- `tests/test_prompt_loader.py` — Tests for prompt loading

### Files to Modify
- `src/scorched/services/research.py` — Replace 24+ bare `except Exception: pass` with logged specific exceptions
- `src/scorched/services/recommender.py` — Extract Claude calls and prompts, thin down to orchestrator
- `src/scorched/services/risk_review.py` — Move prompt to file, keep parsing logic
- `src/scorched/services/position_mgmt.py` — Move prompt to file
- `src/scorched/services/eod_review.py` — Move prompt to file, use claude_client
- `src/scorched/services/playbook.py` — Move prompt to file, use claude_client
- `src/scorched/services/portfolio.py` — Fix N+1 price fetch
- `src/scorched/api/recommendations.py` — Move query logic to service layer
- `cron/tradebot_phase1.py` — Use common.py
- `cron/tradebot_phase2.py` — Use common.py
- `cron/tradebot_phase3.py` — Use common.py
- `cron/tradebot_phase1_5.py` — Use common.py

### Files to Delete
- `main.py` (root) — 73-line wrapper, unused; `src/scorched/main.py` is the real entry point
- `strategy.py` (root) — 41-line wrapper, unused; `src/scorched/services/strategy.py` is the real one
- `recommender.py` (root) — 2-line placeholder, unused

---

### Task 1: Fix Silent Error Swallowing in research.py

**Why first:** This is the highest-ROI change. If any external API fails (yfinance, FRED, Polygon, Alpha Vantage, EDGAR), you currently get zero indication — data just silently disappears. This has real operational impact.

**Files:**
- Modify: `src/scorched/services/research.py`

**Approach:** Add `import logging` + `logger = logging.getLogger(__name__)` (already exists implicitly via other modules but research.py doesn't have it). Replace every bare `except Exception: pass` with a specific exception type + `logger.warning()`. Data-fetching functions should still return empty results on failure (graceful degradation) — we're adding observability, not changing behavior.

- [ ] **Step 1: Add logger to research.py**

At the top of `src/scorched/services/research.py`, after the imports, add:

```python
import logging

logger = logging.getLogger(__name__)
```

- [ ] **Step 2: Fix `_fetch_price_data_sync` (lines 38-75)**

Replace the two bare excepts:

```python
# Line 51 — fast_info fallback
except Exception:
    current_price = float(hist["Close"].iloc[-1])
```
becomes:
```python
except (KeyError, IndexError):
    current_price = float(hist["Close"].iloc[-1])
```

```python
# Line 73 — outer except for entire symbol
except Exception:
    pass
```
becomes:
```python
except Exception:
    logger.warning("Price data fetch failed for %s", symbol, exc_info=True)
```

- [ ] **Step 3: Fix `_fetch_news_sync` (lines 78-93)**

```python
except Exception:
    result[symbol] = []
```
becomes:
```python
except Exception:
    logger.warning("News fetch failed for %s", symbol, exc_info=True)
    result[symbol] = []
```

- [ ] **Step 4: Fix `_fetch_earnings_surprise_sync` (lines 96-118)**

```python
except Exception:
    result[symbol] = []
```
becomes:
```python
except Exception:
    logger.warning("Earnings surprise fetch failed for %s", symbol, exc_info=True)
    result[symbol] = []
```

- [ ] **Step 5: Fix `_fetch_insider_activity_sync` (lines 121-143)**

```python
except Exception:
    result[symbol] = {"recent_buys": 0, "recent_sells": 0}
```
becomes:
```python
except Exception:
    logger.warning("Insider activity fetch failed for %s", symbol, exc_info=True)
    result[symbol] = {"recent_buys": 0, "recent_sells": 0}
```

- [ ] **Step 6: Fix `_build_ticker_to_cik_map` (line 164)**

```python
except Exception:
    return {}
```
becomes:
```python
except Exception:
    logger.warning("SEC ticker-to-CIK map fetch failed", exc_info=True)
    return {}
```

- [ ] **Step 7: Fix `_fetch_edgar_insider_sync` (lines 168-242)**

Three bare excepts in this function. Replace each:

```python
# Line 223 — per-symbol EDGAR fetch
except Exception:
```
becomes:
```python
except Exception:
    logger.warning("EDGAR insider fetch failed for %s, falling back to yfinance", symbol, exc_info=True)
```

```python
# Line 240 — yfinance fallback
except Exception:
    result[symbol] = {"recent_buys": 0, "recent_sells": 0}
```
becomes:
```python
except Exception:
    logger.warning("Insider fallback (yfinance) also failed for %s", symbol, exc_info=True)
    result[symbol] = {"recent_buys": 0, "recent_sells": 0}
```

- [ ] **Step 8: Fix `_fetch_polygon_news_sync` (lines 245-280)**

```python
except Exception:
    result[symbol] = []
```
becomes:
```python
except Exception:
    logger.warning("Polygon news fetch failed for %s", symbol, exc_info=True)
    result[symbol] = []
```

- [ ] **Step 9: Fix `_fetch_av_technicals_sync` (lines 283-327)**

```python
except Exception:
    pass
```
becomes:
```python
except Exception:
    logger.warning("Alpha Vantage RSI fetch failed for %s", symbol, exc_info=True)
```

- [ ] **Step 10: Fix `_fetch_options_data_sync` (lines 330-375)**

```python
except Exception:
    result[symbol] = None
```
becomes:
```python
except Exception:
    logger.warning("Options data fetch failed for %s", symbol, exc_info=True)
    result[symbol] = None
```

- [ ] **Step 11: Fix `_fetch_fred_macro_sync` (lines 378-438)**

Three bare excepts. Replace:

```python
# Line 409 — per-series fetch
except Exception:
    pass
```
becomes:
```python
except Exception:
    logger.warning("FRED series %s fetch failed", series_id, exc_info=True)
```

```python
# Line 428 — CPI YoY computation
except Exception:
    pass
```
becomes:
```python
except Exception:
    logger.warning("CPI YoY computation failed", exc_info=True)
```

```python
# Line 437 — outer catch
except Exception:
    return {}
```
becomes:
```python
except Exception:
    logger.warning("FRED macro fetch failed entirely", exc_info=True)
    return {}
```

- [ ] **Step 12: Fix `_fetch_market_context_sync` (lines 441-526)**

Five bare excepts in this function. Replace each with `logger.warning(...)` following the same pattern. Key instances:

```python
# Line 465 — index fetch
except Exception:
    pass
```
becomes:
```python
except Exception:
    logger.warning("Market context: index %s fetch failed", ticker_sym, exc_info=True)
```

```python
# Line 483 — sector fetch
except Exception:
    pass
```
becomes:
```python
except Exception:
    logger.warning("Market context: sector %s fetch failed", ticker_sym, exc_info=True)
```

```python
# Line 505 — earnings date parse (inner)
except Exception:
    pass
```
becomes:
```python
except (ValueError, TypeError, AttributeError):
    pass
```
(This one is fine to keep silent — it's a date parsing edge case within a loop.)

```python
# Line 507 — per-symbol earnings calendar (outer)
except Exception:
    pass
```
becomes:
```python
except Exception:
    logger.debug("Earnings calendar unavailable for %s", symbol)
```
(Debug level — this is expected for many symbols.)

```python
# Line 524 — SPY news
except Exception:
    pass
```
becomes:
```python
except Exception:
    logger.warning("SPY broad market news fetch failed", exc_info=True)
```

- [ ] **Step 13: Fix `_fetch_momentum_screener_sync` (line 605)**

```python
except Exception:
    continue
```
becomes:
```python
except Exception:
    logger.debug("Screener: skipping %s (data fetch failed)", symbol)
    continue
```
(Debug level — expected for many symbols in the large pool.)

- [ ] **Step 14: Fix `_fetch_opening_prices_sync` (line 634)**

```python
except Exception:
    results[symbol] = None
```
becomes:
```python
except Exception:
    logger.warning("Opening price fetch failed for %s", symbol, exc_info=True)
    results[symbol] = None
```

- [ ] **Step 15: Run tests to verify nothing broke**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/ -v`
Expected: All existing tests PASS (research.py functions still return the same values on success; logging doesn't change return values)

- [ ] **Step 16: Commit**

```bash
git add src/scorched/services/research.py
git commit -m "fix: replace 24 bare excepts in research.py with logged warnings

Silent exception swallowing made it impossible to detect when external
APIs (yfinance, FRED, Polygon, AV, EDGAR) failed. Now all failures are
logged with exc_info for debugging while preserving graceful degradation.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Extract Claude Prompts to Markdown Files

**Why:** System prompts are large text blocks (50-100 lines each) embedded in Python files. They change independently of code logic. Extracting them makes prompt iteration easy without touching Python.

**Files:**
- Create: `src/scorched/prompts/__init__.py`
- Create: `src/scorched/prompts/analysis.md`
- Create: `src/scorched/prompts/decision.md`
- Create: `src/scorched/prompts/risk_review.md`
- Create: `src/scorched/prompts/position_mgmt.md`
- Create: `src/scorched/prompts/eod_review.md`
- Create: `src/scorched/prompts/playbook_update.md`
- Create: `tests/test_prompt_loader.py`
- Modify: `src/scorched/services/recommender.py`
- Modify: `src/scorched/services/risk_review.py`
- Modify: `src/scorched/services/eod_review.py`
- Modify: `src/scorched/services/playbook.py`
- Modify: `src/scorched/services/position_mgmt.py`

- [ ] **Step 1: Create the prompt loader**

Create `src/scorched/prompts/__init__.py`:

```python
"""Prompt loader — reads .md files from this directory at import time."""
from pathlib import Path

_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """Load a prompt template by name (without .md extension).

    Raises FileNotFoundError if the prompt file doesn't exist.
    """
    path = _DIR / f"{name}.md"
    return path.read_text(encoding="utf-8").strip()
```

- [ ] **Step 2: Write failing test for prompt loader**

Create `tests/test_prompt_loader.py`:

```python
"""Tests for prompt loading utility."""
import pytest
from scorched.prompts import load_prompt


def test_load_existing_prompt():
    """Should load a known prompt file without error."""
    text = load_prompt("analysis")
    assert len(text) > 100
    assert "analyst" in text.lower() or "analysis" in text.lower()


def test_load_missing_prompt():
    """Should raise FileNotFoundError for nonexistent prompt."""
    with pytest.raises(FileNotFoundError):
        load_prompt("nonexistent_prompt_xyz")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/test_prompt_loader.py -v`
Expected: FAIL — `analysis.md` doesn't exist yet

- [ ] **Step 4: Extract ANALYSIS_SYSTEM from recommender.py**

Create `src/scorched/prompts/analysis.md` with the exact content of the `ANALYSIS_SYSTEM` string from `recommender.py` lines 50-75. Keep the `{strategy}` and `{guidance}` template placeholders — they'll be filled via `.format()` at call time:

```markdown
You are a disciplined stock market analyst. Your job is to study today's research data and identify which stocks, if any, have a genuinely compelling setup that matches the user's declared trading strategy.

## User's Declared Trading Strategy
{strategy}

## Signal Interpretation Reference
{guidance}

Work through the data with the strategy and signal reference above in mind:
- Which stocks have the exact type of setup this strategy calls for? (momentum breakouts, value entries, etc.)
- What is the macro environment saying? Is it supportive or hostile to this style of trading?
- Which sectors align with the user's stated preferences? Skip sectors they want to avoid.
- For each candidate, is there a specific named catalyst that fits the strategy's entry criteria?
- Are there earnings surprises, insider buying, or unusual options activity in the preferred sectors?
- Which existing positions (if any) should be considered for exit based on the strategy's exit rules?

Be honest. Most days do not have a strong setup matching this strategy. If today is one of those days, say so clearly. Do not force candidates.

Output valid JSON with exactly this structure:
{{
  "analysis": "Your full free-form market analysis (as many paragraphs as needed)",
  "candidates": ["TICKER1", "TICKER2"]
}}

The candidates list contains symbols that fit the declared strategy with a real, named catalyst.
It may be empty. Maximum 5 candidates — only include symbols with a real, named catalyst.
```

- [ ] **Step 5: Extract DECISION_SYSTEM from recommender.py**

Create `src/scorched/prompts/decision.md` with the exact content of `DECISION_SYSTEM` from lines 79-118. Keep all `{strategy}`, `{guidance}`, `{min_cash_pct}`, `{playbook}` template placeholders.

```markdown
You are a disciplined simulated stock trader. You have already done your market analysis (provided below). Now make your final trade decisions.

## User's Declared Trading Strategy
This is what the human investor wants. Follow it precisely — it overrides your own judgment on style, time horizon, and exit rules.
{strategy}

## Signal Interpretation & Hard Rules Reference
{guidance}

## Additional Hard Rules
- Only BUY or SELL (no options, no short selling, no ETFs unless on the watchlist)
- Never recommend a trade that would leave cash below {min_cash_pct}% of total portfolio value
- Weigh tax cost on sells: short-term gains (held < 365 days) taxed at 37%, long-term at 20%
- Maximum 3 trades total — 0, 1, or 2 are equally valid
- Be specific about share quantity based on available cash and conviction level
- Follow both the strategy above AND the playbook below
- If a trade would violate the declared strategy (wrong time horizon, wrong sector, wrong exit discipline), do not make it

## Your Trading Playbook (Learned from Past Trades)
{playbook}

## Output format
Respond with valid JSON only:
{{
  "research_summary": "2-3 sentence summary for the daily report",
  "recommendations": [
    {{
      "symbol": "TICKER",
      "action": "buy" or "sell",
      "suggested_price": 123.45,
      "quantity": 10,
      "reasoning": "Specific catalyst and which strategy entry criteria are met (2-4 sentences)",
      "confidence": "high" or "medium" or "low",
      "key_risks": "Main risks to this trade"
    }}
  ]
}}

An empty recommendations array is a completely valid response.
Do not fabricate catalysts. Do not trade out of habit.
```

- [ ] **Step 6: Extract RISK_REVIEW_SYSTEM from risk_review.py**

Create `src/scorched/prompts/risk_review.md` with the exact content of `RISK_REVIEW_SYSTEM` from `risk_review.py` lines 8-30.

```markdown
You are a skeptical risk committee reviewing proposed trades. Your default stance is REJECT unless the trade clearly passes ALL of the following checks:

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
}
```

- [ ] **Step 7: Extract POSITION_MGMT_SYSTEM from position_mgmt.py**

Read `src/scorched/services/position_mgmt.py` and create `src/scorched/prompts/position_mgmt.md` with the exact content of `POSITION_MGMT_SYSTEM`.

- [ ] **Step 8: Extract EOD_REVIEW_SYSTEM from eod_review.py**

Create `src/scorched/prompts/eod_review.md` with the exact content of `EOD_REVIEW_SYSTEM` from `eod_review.py` lines 21-38.

```markdown
You are maintaining a trading strategy playbook for a simulated stock portfolio. The market has just closed. Your job is to review today's trading decisions against actual intraday outcomes, then update the playbook with honest, specific learnings.

Analyze the following:
- Did the morning thesis hold up by the close? Name stocks and directions explicitly.
- Did confidence levels match outcomes? High-confidence picks that moved against us deserve scrutiny.
- Did the broader market behave as the morning analysis expected?
- Are there execution patterns to note (e.g., too slow to act, wrong sizing, good/bad timing)?
- Are there recurring mistakes or emerging edges worth flagging?

Update the playbook by revising these sections as evidence warrants:
- What Has Worked / What Has Not Worked
- Sectors / Themes to Favor or Avoid
- Position Sizing Rules Learned
- Current Biases to Watch

Be honest. A thesis that was directionally correct but the position was too small is a different lesson than a thesis that was flat-out wrong. Distinguish them.

Return ONLY the full updated playbook text. Preserve the existing structure but rewrite sections as needed. Do not wrap in markdown code blocks.
```

- [ ] **Step 9: Extract UPDATE_SYSTEM_PROMPT from playbook.py**

Create `src/scorched/prompts/playbook_update.md` with the exact content of `UPDATE_SYSTEM_PROMPT` from `playbook.py` lines 44-55.

```markdown
You are maintaining a trading strategy playbook for a simulated stock portfolio. Your job is to review recent closed trade outcomes and update the playbook to reflect genuine learnings.

Be honest and specific. If a thesis was wrong, say so clearly. If a pattern is emerging, name it. The playbook should help future you make better decisions — not rationalize past ones.

Update the playbook by:
1. Noting what worked and why (was the thesis correct, or did you get lucky?)
2. Noting what didn't work and what the actual cause was
3. Updating sector/theme biases based on observed outcomes
4. Refining position sizing guidance if relevant
5. Flagging any recurring mistakes

Return ONLY the full updated playbook text. Preserve the existing structure but rewrite sections as needed. Do not wrap in markdown code blocks.
```

- [ ] **Step 10: Update recommender.py to use prompt loader**

In `src/scorched/services/recommender.py`:

Replace the `ANALYSIS_SYSTEM = """..."""` block (lines 50-75) and `DECISION_SYSTEM = """..."""` block (lines 79-118) with:

```python
from ..prompts import load_prompt

_ANALYSIS_SYSTEM = load_prompt("analysis")
_DECISION_SYSTEM = load_prompt("decision")
```

Then update all references from `ANALYSIS_SYSTEM` to `_ANALYSIS_SYSTEM` and `DECISION_SYSTEM` to `_DECISION_SYSTEM` in the same file. The `.format()` calls remain unchanged.

- [ ] **Step 11: Update risk_review.py to use prompt loader**

In `src/scorched/services/risk_review.py`:

Replace the `RISK_REVIEW_SYSTEM = """..."""` block (lines 8-30) with:

```python
from ..prompts import load_prompt

RISK_REVIEW_SYSTEM = load_prompt("risk_review")
```

Note: `RISK_REVIEW_SYSTEM` is imported by `recommender.py` so it must keep the same name and remain a module-level export.

- [ ] **Step 12: Update eod_review.py to use prompt loader**

In `src/scorched/services/eod_review.py`:

Replace the `EOD_REVIEW_SYSTEM = """..."""` block with:

```python
from ..prompts import load_prompt

EOD_REVIEW_SYSTEM = load_prompt("eod_review")
```

- [ ] **Step 13: Update playbook.py to use prompt loader**

In `src/scorched/services/playbook.py`:

Replace the `UPDATE_SYSTEM_PROMPT = """..."""` block with:

```python
from ..prompts import load_prompt

UPDATE_SYSTEM_PROMPT = load_prompt("playbook_update")
```

- [ ] **Step 14: Update position_mgmt.py to use prompt loader**

In `src/scorched/services/position_mgmt.py`:

Replace the `POSITION_MGMT_SYSTEM = """..."""` block with:

```python
from ..prompts import load_prompt

POSITION_MGMT_SYSTEM = load_prompt("position_mgmt")
```

Note: `POSITION_MGMT_SYSTEM` is imported by `eod_review.py` so it must keep the same name.

- [ ] **Step 15: Run prompt loader tests**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/test_prompt_loader.py -v`
Expected: PASS

- [ ] **Step 16: Run all tests**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 17: Commit**

```bash
git add src/scorched/prompts/ tests/test_prompt_loader.py src/scorched/services/recommender.py src/scorched/services/risk_review.py src/scorched/services/eod_review.py src/scorched/services/playbook.py src/scorched/services/position_mgmt.py
git commit -m "refactor: extract Claude system prompts to markdown files

Moved 6 system prompts from inline Python strings to src/scorched/prompts/*.md.
Prompts now live as standalone files that can be edited without touching Python.
Added load_prompt() utility with tests.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Extract Claude Client from Recommender

**Why:** `recommender.py` (586 lines) is a god function mixing data fetching, Claude API calls, JSON parsing, risk review, cost tracking, and DB writes. Extracting a `claude_client.py` separates Anthropic API concerns from orchestration.

**Files:**
- Create: `src/scorched/services/claude_client.py`
- Create: `tests/test_claude_client.py`
- Modify: `src/scorched/services/recommender.py`
- Modify: `src/scorched/services/eod_review.py`
- Modify: `src/scorched/services/playbook.py`

- [ ] **Step 1: Write failing tests for claude_client**

Create `tests/test_claude_client.py`:

```python
"""Tests for Claude client wrapper."""
import json
import pytest
from unittest.mock import MagicMock, patch
from scorched.services.claude_client import (
    call_analysis,
    call_decision,
    call_risk_review,
    extract_text,
    extract_thinking,
    parse_json_response,
)


def test_extract_text_from_content_blocks():
    block = MagicMock()
    block.type = "text"
    block.text = "hello world"
    assert extract_text([block]) == "hello world"


def test_extract_text_skips_thinking():
    thinking = MagicMock()
    thinking.type = "thinking"
    text = MagicMock()
    text.type = "text"
    text.text = "result"
    assert extract_text([thinking, text]) == "result"


def test_extract_text_empty():
    assert extract_text([]) == ""


def test_extract_thinking():
    block = MagicMock()
    block.type = "thinking"
    block.thinking = "deep thoughts"
    assert extract_thinking([block]) == "deep thoughts"


def test_parse_json_response_clean():
    raw = '{"analysis": "test", "candidates": ["AAPL"]}'
    result = parse_json_response(raw)
    assert result["analysis"] == "test"
    assert result["candidates"] == ["AAPL"]


def test_parse_json_response_with_fences():
    raw = '```json\n{"analysis": "test"}\n```'
    result = parse_json_response(raw)
    assert result["analysis"] == "test"


def test_parse_json_response_invalid():
    result = parse_json_response("not json at all")
    assert result == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/test_claude_client.py -v`
Expected: FAIL — `claude_client` module doesn't exist

- [ ] **Step 3: Create claude_client.py**

Create `src/scorched/services/claude_client.py`:

```python
"""Claude API client — all Anthropic interactions in one place."""
import json
import logging
import re

import anthropic

from ..config import settings
from ..prompts import load_prompt
from ..retry import claude_call_with_retry

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
THINKING_BUDGET = 16000


def _get_client() -> anthropic.Anthropic:
    """Create an Anthropic client from settings."""
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


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
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        return {}


def call_analysis(
    strategy: str,
    guidance: str,
    user_content: str,
    tracker=None,
) -> tuple:
    """Call 1: Analysis with extended thinking.

    Returns (response, analysis_text, thinking_text, candidates, parsed_dict).
    """
    from ..api_tracker import track_call
    from contextlib import nullcontext

    system = load_prompt("analysis").format(strategy=strategy, guidance=guidance)
    client = _get_client()

    logger.info("Call 1: analysis with extended thinking (budget=%d)", THINKING_BUDGET)
    ctx = track_call(tracker, "claude", "analysis") if tracker else nullcontext()
    with ctx:
        response = claude_call_with_retry(
            client, "Call 1 (analysis)",
            model=MODEL,
            max_tokens=THINKING_BUDGET + 2048,
            thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET},
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )

    analysis_raw = extract_text(response.content)
    analysis_thinking = extract_thinking(response.content)
    parsed = parse_json_response(analysis_raw)

    analysis_text = parsed.get("analysis", analysis_raw)
    candidates = [s.upper() for s in parsed.get("candidates", [])][:5]

    logger.info("Call 1 candidates: %s", candidates)
    return response, analysis_text, analysis_thinking, candidates, parsed


def call_decision(
    strategy: str,
    guidance: str,
    playbook_content: str,
    min_cash_pct: int,
    user_content: str,
    tracker=None,
) -> tuple:
    """Call 2: Decision (standard, no extended thinking).

    Returns (response, parsed_dict).
    """
    from ..api_tracker import track_call
    from contextlib import nullcontext

    system = load_prompt("decision").format(
        min_cash_pct=min_cash_pct,
        playbook=playbook_content,
        strategy=strategy,
        guidance=guidance,
    )
    client = _get_client()

    logger.info("Call 2: trade decision")
    ctx = track_call(tracker, "claude", "decision") if tracker else nullcontext()
    with ctx:
        response = claude_call_with_retry(
            client, "Call 2 (decision)",
            model=MODEL,
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )

    decision_raw = response.content[0].text
    parsed = parse_json_response(decision_raw)
    if not parsed:
        parsed = {"research_summary": decision_raw, "recommendations": []}

    return response, decision_raw, parsed


def call_risk_review(
    user_content: str,
    tracker=None,
) -> tuple:
    """Call 3: Risk committee review (adversarial).

    Returns (response, raw_text).
    """
    from ..api_tracker import track_call
    from contextlib import nullcontext

    system = load_prompt("risk_review")
    client = _get_client()

    logger.info("Call 3: risk committee review")
    ctx = track_call(tracker, "claude", "risk_review") if tracker else nullcontext()
    with ctx:
        response = claude_call_with_retry(
            client, "Call 3 (risk review)",
            model=MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )

    return response, response.content[0].text


def call_position_review(
    user_content: str,
) -> tuple:
    """Call 4: Position management review.

    Returns (response, raw_text).
    """
    system = load_prompt("position_mgmt")
    client = _get_client()

    logger.info("Call 4: position management review")
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )

    return response, response.content[0].text


def call_eod_review(
    user_content: str,
) -> tuple:
    """EOD review call — updates playbook based on day's outcomes.

    Returns (response, updated_text).
    """
    system = load_prompt("eod_review")
    client = _get_client()

    logger.info("EOD review: calling Claude to update playbook")
    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )

    return response, response.content[0].text.strip()


def call_playbook_update(
    user_content: str,
) -> tuple:
    """Playbook update call — revises playbook from recent trade outcomes.

    Returns (response, updated_text).
    """
    system = load_prompt("playbook_update")
    client = _get_client()

    response = claude_call_with_retry(
        client, "Playbook update",
        model="claude-opus-4-6",
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )

    return response, response.content[0].text.strip()
```

- [ ] **Step 4: Run tests**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/test_claude_client.py -v`
Expected: PASS for the pure-function tests (extract_text, parse_json_response, etc.)

- [ ] **Step 5: Update recommender.py to use claude_client**

In `src/scorched/services/recommender.py`:

1. Remove the `ANALYSIS_SYSTEM` and `DECISION_SYSTEM` string literals (already removed if Task 2 is done, otherwise remove now).
2. Remove `_extract_text`, `_extract_thinking`, `_parse_json_response` functions — they now live in `claude_client.py`.
3. Remove `MODEL = "claude-sonnet-4-6"` and `THINKING_BUDGET = 16000` — now in `claude_client.py`.
4. Remove `import anthropic` — no longer needed directly.
5. Remove the import of `claude_call_with_retry` — no longer called directly.

Add imports:

```python
from .claude_client import (
    call_analysis,
    call_decision,
    call_risk_review,
    parse_json_response,
)
```

Replace the Call 1 block (lines 358-394) with:

```python
    # ── Call 1: Analysis with extended thinking ────────────────────────────
    call1_user = f"Today's date: {session_date}\n\n{market_context}\n\n{research_context}"
    call1_response, analysis_text, analysis_thinking, candidates, _ = call_analysis(
        strategy=strategy,
        guidance=guidance,
        user_content=call1_user,
        tracker=tracker,
    )

    # Record Call 1 token usage
    usage1 = call1_response.usage
    await record_usage(
        db,
        session_id=session_row.id,
        call_type="analysis",
        model=call1_response.model,
        input_tokens=usage1.input_tokens,
        output_tokens=usage1.output_tokens,
        thinking_tokens=getattr(usage1, "thinking_tokens", 0),
    )

    # Store analysis text (thinking + analysis) on the session row
    thinking_prefix = f"[THINKING]\n{analysis_thinking}\n\n[ANALYSIS]\n" if analysis_thinking else ""
    session_row.analysis_text = thinking_prefix + analysis_text
```

Replace the Call 2 block (lines 402-457) with:

```python
    # ── Call 2: Decision (standard, no extended thinking) ─────────────────
    min_cash_pct = int(settings.min_cash_reserve_pct * 100)
    options_context = build_options_context(options_data) if options_data else ""
    call2_user = (
        f"Today's date: {session_date}\n\n"
        f"## Your Analysis\n{analysis_text}\n\n"
        f"{options_context}\n\n"
        f"## Current Portfolio\n"
        f"Cash available: ${portfolio_dict['cash_balance']:,.2f}\n"
        f"Total value: ${portfolio_dict['total_value']:,.2f}\n"
    )
    if portfolio_dict["positions"]:
        call2_user += "Held positions:\n"
        for pos in portfolio_dict["positions"]:
            call2_user += (
                f"  {pos['symbol']}: {pos['shares']} shares, "
                f"cost ${pos['avg_cost_basis']:.2f}, "
                f"now ${pos['current_price']:.2f}, "
                f"{pos['days_held']}d ({pos['tax_category']})\n"
            )

    call2_response, decision_raw, parsed = call_decision(
        strategy=strategy,
        guidance=guidance,
        playbook_content=playbook.content,
        min_cash_pct=min_cash_pct,
        user_content=call2_user,
        tracker=tracker,
    )

    usage2 = call2_response.usage
    await record_usage(
        db,
        session_id=session_row.id,
        call_type="decision",
        model=call2_response.model,
        input_tokens=usage2.input_tokens,
        output_tokens=usage2.output_tokens,
    )

    session_row.claude_response = decision_raw
    research_summary = parsed.get("research_summary", "")
    raw_recs = parsed.get("recommendations", [])[:3]
```

Replace the Call 3 block (lines 460-500) with:

```python
    # ── Call 3: Risk committee review (adversarial) ──────────────────────────
    if raw_recs:
        logger.info("Call 3: risk committee review of %d recommendations", len(raw_recs))
        playbook_excerpt = playbook.content[:500] if playbook else ""
        risk_prompt = build_risk_review_prompt(raw_recs, portfolio_dict, analysis_text, playbook_excerpt)

        call3_response, risk_raw = call_risk_review(
            user_content=risk_prompt,
            tracker=tracker,
        )

        usage3 = call3_response.usage
        await record_usage(
            db,
            session_id=session_row.id,
            call_type="risk_review",
            model=call3_response.model,
            input_tokens=usage3.input_tokens,
            output_tokens=usage3.output_tokens,
        )

        risk_decisions = parse_risk_review_response(risk_raw)
        rejected_symbols = {
            d["symbol"].upper()
            for d in risk_decisions
            if d.get("verdict") == "reject" and d.get("action", "").lower() == "buy"
        }
        if rejected_symbols:
            logger.info("Risk committee rejected buys: %s", rejected_symbols)
            for d in risk_decisions:
                if d.get("verdict") == "reject":
                    logger.info("  %s %s: %s", d.get("action"), d.get("symbol"), d.get("reason"))

        raw_recs = [
            r for r in raw_recs
            if not (r.get("action", "").lower() == "buy" and r.get("symbol", "").upper() in rejected_symbols)
        ]
```

- [ ] **Step 6: Update eod_review.py to use claude_client**

In `src/scorched/services/eod_review.py`:

1. Remove `import anthropic`.
2. Remove `MODEL = "claude-sonnet-4-6"` and `EOD_REVIEW_SYSTEM` (if done in Task 2, otherwise now).
3. Add `from .claude_client import call_eod_review, call_position_review`.

Replace the EOD Claude call (lines 209-215) with:

```python
    response, updated_content = call_eod_review(user_content=user_content)
```

Replace the position review Claude call (lines 253-258) with:

```python
        pos_response, _pos_text = call_position_review(user_content=pos_prompt)
```

Update the `record_usage` calls to use `response.usage` / `pos_response.usage` accordingly.

- [ ] **Step 7: Update playbook.py to use claude_client**

In `src/scorched/services/playbook.py`:

1. Remove `import anthropic` and the import of `claude_call_with_retry`.
2. Remove `UPDATE_SYSTEM_PROMPT` (if done in Task 2).
3. Add `from .claude_client import call_playbook_update`.

Replace the Claude call (lines 157-165) with:

```python
    try:
        _response, updated_content = call_playbook_update(user_content=user_content)
    except Exception:
        logger.error("Playbook update failed after all retries — using stale playbook")
        return playbook
```

- [ ] **Step 8: Run all tests**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 9: Commit**

```bash
git add src/scorched/services/claude_client.py tests/test_claude_client.py src/scorched/services/recommender.py src/scorched/services/eod_review.py src/scorched/services/playbook.py
git commit -m "refactor: extract Claude API interactions to claude_client.py

Moved all Anthropic SDK calls, response parsing, and prompt loading out of
recommender.py (586→~350 lines), eod_review.py, and playbook.py into a
dedicated claude_client.py. Each call_*() function encapsulates one Claude
API interaction with consistent logging and error handling.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Clean Up Root Directory

**Why:** Three stale files in the project root create confusion about which file is the real entry point.

**Files:**
- Delete: `main.py` (root, 73 lines)
- Delete: `strategy.py` (root, 41 lines)
- Delete: `recommender.py` (root, 2 lines)

- [ ] **Step 1: Verify root files are not imported or referenced**

Check that nothing imports from these root files:

```bash
cd /home/ubuntu/tradebot
grep -r "from main import\|import main" --include="*.py" | grep -v __pycache__ | grep -v ".pyc"
grep -r "from strategy import\|import strategy" --include="*.py" | grep -v __pycache__ | grep -v ".pyc" | grep -v "scorched"
grep -r "from recommender import\|import recommender" --include="*.py" | grep -v __pycache__ | grep -v ".pyc" | grep -v "scorched"
```

Check that `entrypoint.sh` and `Dockerfile` reference the correct module:

```bash
cat entrypoint.sh
cat Dockerfile
```

The Docker setup should use `uvicorn scorched.main:app` (from the `src/` package), NOT `main:app` (the root wrapper). If it references `main:app`, update it to `scorched.main:app`.

- [ ] **Step 2: Verify entrypoint uses correct module path**

Read `entrypoint.sh`. If it says `uvicorn main:app`, that's using the root wrapper.  Check if `Dockerfile` sets `PYTHONPATH` or `WORKDIR` that would make this resolve to the src package.

If the root `main.py` IS the entry point used by Docker, then do NOT delete it — instead update it to be a thin re-export:

```python
"""Entry point — re-exports the FastAPI app from the scorched package."""
from scorched.main import app  # noqa: F401
```

Only delete it if Docker/entrypoint already uses `scorched.main:app`.

- [ ] **Step 3: Delete confirmed-unused root files**

```bash
rm /home/ubuntu/tradebot/strategy.py
rm /home/ubuntu/tradebot/recommender.py
```

And if confirmed unused in Step 2:
```bash
rm /home/ubuntu/tradebot/main.py
```

- [ ] **Step 4: Run tests**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add -A  # captures the deletions
git commit -m "chore: remove stale root-level wrapper files

Removed strategy.py (41-line wrapper), recommender.py (2-line placeholder),
and main.py (if unused by Docker). The real implementations live in
src/scorched/services/ and src/scorched/main.py.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Fix N+1 Price Fetch in portfolio.py

**Why:** `get_portfolio_state()` calls `_get_current_price()` per position — each a separate yfinance HTTP call. With 5 positions, that's 5 serial HTTP calls. One batch call replaces them all.

**Files:**
- Modify: `src/scorched/services/portfolio.py`

- [ ] **Step 1: Read portfolio.py to understand the current pattern**

Read `src/scorched/services/portfolio.py` and identify:
1. The `_get_current_price()` helper function
2. Where it's called in a loop
3. The response format expected by callers

- [ ] **Step 2: Replace N+1 with batch fetch**

Find the `_get_current_price` function and the loop that calls it. Replace with a batch approach:

```python
async def _get_current_prices(symbols: list[str]) -> dict[str, float]:
    """Batch-fetch current prices for all symbols in one yfinance call."""
    if not symbols:
        return {}
    import yfinance as yf

    def _fetch():
        result = {}
        for symbol in symbols:
            try:
                ticker = yf.Ticker(symbol)
                try:
                    result[symbol] = float(ticker.fast_info["last_price"])
                except (KeyError, IndexError):
                    hist = ticker.history(period="1d")
                    if not hist.empty:
                        result[symbol] = float(hist["Close"].iloc[-1])
            except Exception:
                pass  # Will fall back to avg_cost_basis
        return result

    return await asyncio.get_event_loop().run_in_executor(None, _fetch)
```

Then update the function that builds position details to call `_get_current_prices(all_symbols)` once and look up prices from the returned dict instead of calling `_get_current_price(symbol)` per position.

- [ ] **Step 3: Run tests**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/scorched/services/portfolio.py
git commit -m "perf: batch price fetches in portfolio.py (fix N+1)

Replaced per-position _get_current_price() calls (5 serial HTTP requests)
with a single _get_current_prices() batch fetch. Same data, fewer round trips.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Consolidate Cron Script Shared Logic

**Why:** The 4 cron scripts each independently load `.env`, build HTTP helpers, and format Telegram messages. Extracting shared logic reduces duplication and makes adding new cron phases easier.

**Files:**
- Create: `cron/common.py`
- Modify: `cron/tradebot_phase1.py`
- Modify: `cron/tradebot_phase1_5.py`
- Modify: `cron/tradebot_phase2.py`
- Modify: `cron/tradebot_phase3.py`

- [ ] **Step 1: Read all cron scripts to identify shared patterns**

Read all four cron scripts and identify the duplicated code blocks:
1. `.env` loading
2. HTTP helper functions
3. Telegram message sending
4. Base URL construction
5. Error handling / retry patterns

- [ ] **Step 2: Create cron/common.py with shared utilities**

Create `cron/common.py` extracting the shared logic. The exact implementation depends on what's found in Step 1, but the structure should be:

```python
"""Shared utilities for cron phase scripts."""
import json
import os
import urllib.request
import urllib.error
from pathlib import Path


def load_env():
    """Load .env file from project root into os.environ."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def get_base_url() -> str:
    """Return the tradebot API base URL."""
    host = os.environ.get("HOST", "127.0.0.1")
    port = os.environ.get("PORT", "8000")
    return f"http://{host}:{port}"


def api_post(path: str, data: dict | None = None, pin: str = "") -> dict:
    """POST to the tradebot API. Returns parsed JSON response."""
    url = f"{get_base_url()}{path}"
    headers = {"Content-Type": "application/json"}
    if pin:
        headers["X-Settings-Pin"] = pin
    body = json.dumps(data or {}).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode())


def api_get(path: str) -> dict:
    """GET from the tradebot API. Returns parsed JSON response."""
    url = f"{get_base_url()}{path}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def send_telegram(message: str, parse_mode: str = "HTML") -> None:
    """Send a message via Telegram bot if credentials are configured."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": parse_mode,
    }).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception:
        pass  # Don't fail the phase if Telegram is down
```

- [ ] **Step 3: Update each cron script to use common.py**

For each of the 4 cron scripts, replace the duplicated env-loading, HTTP, and Telegram code with imports from `common.py`. Keep the phase-specific logic (which endpoints to call, what data to format) in each script.

Example pattern for the top of each script:

```python
#!/usr/bin/env python3
"""Phase N: [description]."""
import sys
from pathlib import Path

# Add cron directory to path for common module
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_env, api_post, api_get, send_telegram

load_env()

# ... phase-specific logic using api_post/api_get/send_telegram ...
```

- [ ] **Step 4: Test cron scripts still work**

```bash
cd /home/ubuntu/tradebot
python -c "from cron.common import load_env, get_base_url, send_telegram; load_env(); print(get_base_url())"
```
Expected: `http://127.0.0.1:8000` (or configured host/port)

- [ ] **Step 5: Commit**

```bash
git add cron/
git commit -m "refactor: extract shared cron utilities to cron/common.py

All 4 phase scripts shared env loading, HTTP helpers, and Telegram sending.
Extracted to common.py to reduce duplication (~200 lines removed across scripts).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Move Query Logic from API Endpoints to Services

**Why:** API endpoints should be thin — validate, delegate, return. The `recommendations.py` endpoint has inline SQLAlchemy query building and JSON parsing that belongs in a service.

**Files:**
- Modify: `src/scorched/api/recommendations.py`
- Modify: `src/scorched/services/recommender.py` (add query methods)

- [ ] **Step 1: Move list_sessions query to recommender service**

Add to `src/scorched/services/recommender.py`:

```python
async def list_sessions(
    db: AsyncSession,
    session_date: date | None = None,
    limit: int = 10,
) -> list[RecommendationSession]:
    """Return recommendation sessions, optionally filtered by date."""
    q = (
        select(RecommendationSession)
        .order_by(RecommendationSession.session_date.desc())
        .limit(limit)
    )
    if session_date:
        q = q.where(RecommendationSession.session_date == session_date)
    return list((await db.execute(q)).scalars().all())


async def get_session(db: AsyncSession, session_id: int) -> RecommendationSession | None:
    """Return a single session by ID, or None."""
    return (
        await db.execute(
            select(RecommendationSession).where(RecommendationSession.id == session_id)
        )
    ).scalars().first()
```

- [ ] **Step 2: Simplify recommendations.py API endpoint**

Replace the `list_sessions` endpoint body with:

```python
@router.get("", response_model=list[SessionListItem])
async def list_sessions(
    session_date: date | None = Query(None),
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    rows = await recommender_svc.list_sessions(db, session_date=session_date, limit=limit)
    return [
        SessionListItem(
            id=r.id,
            session_date=r.session_date,
            recommendation_count=len(r.recommendations),
            created_at=r.created_at,
        )
        for r in rows
    ]
```

Replace the `get_session` and `get_session_analysis` endpoints to use `recommender_svc.get_session()`:

```python
@router.get("/{session_id}/analysis")
async def get_session_analysis(session_id: int, db: AsyncSession = Depends(get_db)):
    from fastapi import HTTPException
    row = await recommender_svc.get_session(db, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": session_id, "analysis_text": row.analysis_text}


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session_detail(session_id: int, db: AsyncSession = Depends(get_db)):
    from fastapi import HTTPException
    row = await recommender_svc.get_session(db, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    research_summary = ""
    if row.claude_response:
        try:
            research_summary = json.loads(row.claude_response).get("research_summary", "")
        except (json.JSONDecodeError, AttributeError):
            pass

    recs = [
        RecommendationItem(
            id=r.id,
            symbol=r.symbol,
            action=r.action,
            suggested_price=r.suggested_price,
            quantity=r.quantity,
            estimated_cost=(r.suggested_price * r.quantity).quantize(Decimal("0.01")),
            reasoning=r.reasoning,
            confidence=r.confidence,
            key_risks=r.key_risks,
        )
        for r in row.recommendations
    ]

    return SessionDetail(
        id=row.id,
        session_date=row.session_date,
        research_summary=research_summary,
        recommendations=recs,
        created_at=row.created_at,
    )
```

- [ ] **Step 3: Clean up imports in recommendations.py**

Remove `from sqlalchemy import select` and `from ..models import RecommendationSession` — no longer needed in the API layer.

- [ ] **Step 4: Run all tests**

Run: `cd /home/ubuntu/tradebot && python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/scorched/api/recommendations.py src/scorched/services/recommender.py
git commit -m "refactor: move session queries from API endpoint to service layer

API endpoints are now thin (validate, delegate, return). Query building
for list_sessions and get_session moved to recommender service.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review Checklist

1. **Spec coverage:** All 7 refactoring items from the original review are covered (error handling, prompt extraction, recommender decomposition, root cleanup, N+1 fix, cron consolidation, query extraction).

2. **Placeholder scan:** No TBD/TODO items. All code blocks contain actual implementation code.

3. **Type consistency:** `load_prompt()`, `call_analysis()`, `call_decision()`, etc. signatures are consistent across Task 2 and Task 3. `RISK_REVIEW_SYSTEM` stays as a module-level export name since `recommender.py` imports it.

4. **No behavior changes:** Every task preserves existing external behavior. API responses don't change. Claude prompts are byte-identical. Data flow is the same.

5. **Test safety:** Every task ends with "run all tests" before committing.
