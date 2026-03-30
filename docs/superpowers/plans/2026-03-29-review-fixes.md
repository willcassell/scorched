# Code Review Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the six highest-priority issues identified in the deep code review — three correctness fixes (hard stop, async sleep, retry consistency) then three quality improvements (context size warning, bak cleanup, EOD prompt structure).

**Architecture:** All changes are surgical — no new files except tests. Task 1 adds a hard programmatic exit to the intraday endpoint for positions past -5%. Task 2 converts the retry helper from blocking `time.sleep` to async `asyncio.sleep`. Task 3 wraps the two unprotected Claude calls in the existing retry helper. Tasks 4-6 are independent quality improvements.

**Tech Stack:** Python 3.12, FastAPI, pytest, anthropic SDK

---

## File Map

| File | Changes |
|------|---------|
| `src/scorched/api/intraday.py` | Add hard -5% auto-exit before Claude evaluation |
| `src/scorched/retry.py` | Convert to async (`asyncio.sleep`, `async def`) |
| `src/scorched/services/claude_client.py` | Update all callers of retry; wrap `call_position_review`, `call_eod_review`, `call_intraday_exit` in retry |
| `src/scorched/services/eod_review.py` | Await the now-async call wrappers |
| `src/scorched/services/recommender.py` | Await the now-async retry calls |
| `src/scorched/prompts/eod_review.md` | Add structured outcome taxonomy |
| `src/scorched/static/dashboard.html.bak` | Delete |
| `tests/test_retry.py` | New — tests for async retry |
| `tests/test_intraday.py` | Add tests for hard stop logic |
| `tests/test_intraday_endpoint.py` | New — tests for the hard stop in the API endpoint |

---

## Phase 1: Correctness Fixes

### Task 1: Hard -5% Stop Loss in Code

The intraday endpoint currently sends ALL triggered positions to Claude for evaluation, even when a position has crossed the -5% hard stop threshold. The -5% rule is documented as "Hard Rules — Never Break" in `analyst_guidance.md` but is enforced via LLM interpretation, not code. Fix: execute the sell directly when `drop_from_entry >= 5%`, bypassing Claude entirely.

**Files:**
- Modify: `src/scorched/api/intraday.py:50-125`
- Modify: `tests/test_intraday.py` (add hard stop unit test)
- Create: `tests/test_intraday_endpoint.py`

- [ ] **Step 1: Write the unit test for hard stop detection**

Add a helper function test to `tests/test_intraday.py`:

```python
class TestHardStopDetection:
    def test_identifies_hard_stop_at_5pct(self):
        """Position down exactly 5% should trigger hard stop."""
        result = check_position_drop_from_entry(Decimal("95"), Decimal("100"), 5.0)
        # At exactly 5%, the existing function returns passed=True (uses >)
        # Hard stop should use >= so exactly 5% also triggers
        assert result.passed is True  # confirms current behavior: > not >=

    def test_identifies_hard_stop_past_5pct(self):
        """Position down more than 5% should trigger hard stop."""
        result = check_position_drop_from_entry(Decimal("94"), Decimal("100"), 5.0)
        assert result.passed is False
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python3 -m pytest tests/test_intraday.py::TestHardStopDetection -v`
Expected: PASS (these test existing behavior)

- [ ] **Step 3: Write the endpoint test for hard stop auto-exit**

Create `tests/test_intraday_endpoint.py`:

```python
"""Tests for the intraday evaluate endpoint — hard stop logic."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from scorched.main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestHardStopAutoExit:
    """When a position is down >= hard_stop_pct from entry, sell without calling Claude."""

    def _trigger_payload(self, current_price: float, entry_price: float):
        return {
            "triggers": [{
                "symbol": "ACME",
                "trigger_reasons": ["Down 6.0% from entry $100.00 (threshold: 5.0%)"],
                "current_price": current_price,
                "entry_price": entry_price,
                "today_open": 97.0,
                "today_high": 98.0,
                "today_low": current_price,
                "days_held": 3,
                "shares": 10.0,
                "original_reasoning": "momentum play",
            }],
            "market_context": {"spy_change_pct": -0.5, "vix_current": 22.0},
        }

    @patch("scorched.api.intraday.get_broker")
    @patch("scorched.api.intraday.call_intraday_exit")
    @patch("scorched.api.intraday.record_usage", new_callable=AsyncMock)
    def test_hard_stop_skips_claude(self, mock_record, mock_claude, mock_broker, client):
        """Position down 6% should sell without calling Claude."""
        mock_broker_instance = MagicMock()
        mock_broker.return_value = mock_broker_instance
        mock_broker_instance.submit_sell = AsyncMock(return_value={
            "status": "filled",
            "filled_avg_price": Decimal("94.00"),
            "trade_id": 1,
            "realized_gain": Decimal("-60.00"),
        })

        response = client.post("/api/v1/intraday/evaluate", json=self._trigger_payload(94.0, 100.0))

        assert response.status_code == 200
        data = response.json()
        assert len(data["decisions"]) == 1
        assert data["decisions"][0]["action"] == "exit_full"
        assert "hard stop" in data["decisions"][0]["reasoning"].lower()

        # Claude should NOT have been called
        mock_claude.assert_not_called()

    @patch("scorched.api.intraday.get_broker")
    @patch("scorched.api.intraday.call_intraday_exit")
    @patch("scorched.api.intraday.record_usage", new_callable=AsyncMock)
    def test_below_hard_stop_calls_claude(self, mock_record, mock_claude, mock_broker, client):
        """Position down 3% should go to Claude for evaluation."""
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_response.model = "claude-sonnet-4-6"
        mock_claude.return_value = (mock_response, '{"action": "hold", "reasoning": "thesis intact"}')

        response = client.post("/api/v1/intraday/evaluate", json=self._trigger_payload(97.0, 100.0))

        assert response.status_code == 200
        data = response.json()
        assert data["decisions"][0]["action"] == "hold"

        # Claude SHOULD have been called
        mock_claude.assert_called_once()
```

- [ ] **Step 4: Run endpoint tests to verify they fail**

Run: `python3 -m pytest tests/test_intraday_endpoint.py -v`
Expected: FAIL — `test_hard_stop_skips_claude` fails because all positions currently go to Claude

- [ ] **Step 5: Implement the hard stop in the evaluate endpoint**

Modify `src/scorched/api/intraday.py`. Add hard stop detection before the Claude call loop. The threshold comes from `strategy.json` (default 5.0%):

```python
"""Intraday monitoring endpoint — evaluates triggered positions via Claude."""
import json
import logging
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..broker import get_broker
from ..cost import record_usage
from ..database import get_db
from ..schemas import (
    IntradayDecision,
    IntradayEvaluateRequest,
    IntradayEvaluateResponse,
)
from ..services.claude_client import call_intraday_exit, parse_json_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/intraday", tags=["intraday"])

STRATEGY_PATH = Path(__file__).resolve().parent.parent.parent.parent / "strategy.json"


def _load_hard_stop_pct() -> float:
    """Load hard stop threshold from strategy.json, default 5.0%."""
    try:
        strategy = json.loads(STRATEGY_PATH.read_text())
        return strategy.get("intraday_monitor", {}).get("position_drop_from_entry_pct", 5.0)
    except (OSError, json.JSONDecodeError):
        return 5.0


def _is_hard_stop(trigger, hard_stop_pct: float) -> bool:
    """Return True if position has dropped >= hard_stop_pct from entry."""
    if trigger.entry_price <= 0:
        return False
    drop_pct = float((trigger.entry_price - trigger.current_price) / trigger.entry_price * 100)
    return drop_pct >= hard_stop_pct


def _build_exit_prompt(trigger, market_ctx) -> str:
    """Build the user prompt for a single triggered position."""
    pct_change = float((trigger.current_price - trigger.entry_price) / trigger.entry_price * 100) if trigger.entry_price else 0
    lines = [
        f"Position: {trigger.symbol}, {trigger.shares} shares, "
        f"entry ${trigger.entry_price}, current ${trigger.current_price} "
        f"({pct_change:+.1f}%)",
        "Triggers fired:",
    ]
    for reason in trigger.trigger_reasons:
        lines.append(f"  - {reason}")
    lines.append(
        f"Today's action: Opened ${trigger.today_open}, "
        f"high ${trigger.today_high}, low ${trigger.today_low}"
    )
    lines.append(f"SPY today: {market_ctx.spy_change_pct:+.1f}%")
    lines.append(f"VIX: {market_ctx.vix_current:.1f}")
    lines.append(f"Days held: {trigger.days_held}")
    if trigger.original_reasoning:
        lines.append(f"Original thesis: {trigger.original_reasoning[:300]}")
    lines.append(
        "\nShould this position be exited? Respond with JSON: "
        '{"action": "exit_full"|"exit_partial"|"hold", "partial_pct": null or int, "reasoning": "..."}'
    )
    return "\n".join(lines)


async def _execute_sell(trigger, db, reasoning: str) -> IntradayDecision:
    """Execute a sell for a triggered position and return the decision."""
    broker = get_broker(db)
    trade_result = None
    action = "exit_full"

    try:
        result = await broker.submit_sell(
            symbol=trigger.symbol,
            qty=trigger.shares,
            limit_price=trigger.current_price,
            recommendation_id=None,
        )
        if result["status"] == "filled":
            trade_result = {
                "trade_id": result.get("trade_id"),
                "shares": float(trigger.shares),
                "execution_price": float(result["filled_avg_price"]),
                "realized_gain": float(result.get("realized_gain") or 0),
            }
            logger.info(
                "Intraday exit executed: SELL %s %s shares @ %s",
                trigger.symbol, trigger.shares, result["filled_avg_price"],
            )
    except Exception as e:
        logger.error("Intraday sell failed for %s: %s", trigger.symbol, e)
        reasoning += f" [SELL FAILED: {e}]"
        action = "hold"

    return IntradayDecision(
        symbol=trigger.symbol,
        action=action,
        reasoning=reasoning,
        trade_result=trade_result,
    )


@router.post("/evaluate", response_model=IntradayEvaluateResponse)
async def evaluate_triggers(
    body: IntradayEvaluateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Evaluate triggered positions via Claude and execute exits."""
    decisions = []
    hard_stop_pct = _load_hard_stop_pct()

    for trigger in body.triggers:
        # Hard stop: if position is down >= threshold, sell immediately without Claude
        if _is_hard_stop(trigger, hard_stop_pct):
            drop_pct = float((trigger.entry_price - trigger.current_price) / trigger.entry_price * 100)
            reasoning = (
                f"Hard stop triggered: position down {drop_pct:.1f}% from entry "
                f"(>= {hard_stop_pct:.1f}% threshold). Auto-exit without Claude evaluation."
            )
            logger.info("HARD STOP %s: down %.1f%% — auto-selling", trigger.symbol, drop_pct)

            decision = await _execute_sell(trigger, db, reasoning)
            decisions.append(decision)
            continue

        # Normal path: Claude evaluates
        prompt = _build_exit_prompt(trigger, body.market_context)

        response, raw_text = call_intraday_exit(prompt)

        # Record usage
        usage = response.usage
        await record_usage(
            db,
            session_id=None,
            call_type="intraday_exit",
            model=response.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

        parsed = parse_json_response(raw_text)
        action = parsed.get("action", "hold")
        reasoning = parsed.get("reasoning", raw_text[:200])
        partial_pct = parsed.get("partial_pct")

        logger.info(
            "Intraday %s %s: %s — %s",
            trigger.symbol, action, reasoning[:100],
            trigger.trigger_reasons,
        )

        trade_result = None

        if action in ("exit_full", "exit_partial"):
            sell_qty = trigger.shares
            if action == "exit_partial" and partial_pct:
                sell_qty = (trigger.shares * Decimal(str(partial_pct)) / 100).quantize(Decimal("1"))
                sell_qty = max(sell_qty, Decimal("1"))

            broker = get_broker(db)
            try:
                result = await broker.submit_sell(
                    symbol=trigger.symbol,
                    qty=sell_qty,
                    limit_price=trigger.current_price,
                    recommendation_id=None,
                )
                if result["status"] == "filled":
                    trade_result = {
                        "trade_id": result.get("trade_id"),
                        "shares": float(sell_qty),
                        "execution_price": float(result["filled_avg_price"]),
                        "realized_gain": float(result.get("realized_gain") or 0),
                    }
                    logger.info(
                        "Intraday exit executed: SELL %s %s shares @ %s",
                        trigger.symbol, sell_qty, result["filled_avg_price"],
                    )
            except Exception as e:
                logger.error("Intraday sell failed for %s: %s", trigger.symbol, e)
                reasoning += f" [SELL FAILED: {e}]"
                action = "hold"

        decisions.append(IntradayDecision(
            symbol=trigger.symbol,
            action=action,
            reasoning=reasoning,
            trade_result=trade_result,
        ))

    await db.commit()
    return IntradayEvaluateResponse(decisions=decisions)
```

- [ ] **Step 6: Run all tests to verify**

Run: `python3 -m pytest tests/test_intraday_endpoint.py tests/test_intraday.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add src/scorched/api/intraday.py tests/test_intraday.py tests/test_intraday_endpoint.py
git commit -m "fix: enforce -5% hard stop programmatically before Claude evaluation

Positions that have dropped >= the hard stop threshold (default 5%) are
now auto-sold without calling Claude. This makes the 'Hard Rules — Never
Break' in analyst_guidance.md a code-level assertion, not an LLM judgment.
Saves LLM cost and eliminates the risk of Claude rationalizing a hold."
```

---

### Task 2: Convert retry.py from blocking to async

`claude_call_with_retry` uses `time.sleep()` which blocks the entire asyncio event loop for up to 60 seconds on retry. The function is called from async context in `recommender.py` via `call_analysis`, `call_decision`, and `call_risk_review`. Fix: make it `async def` with `asyncio.sleep`.

**Files:**
- Modify: `src/scorched/retry.py`
- Modify: `src/scorched/services/claude_client.py` (await the now-async retry)
- Modify: `src/scorched/services/recommender.py` (await calls that are now async)
- Create: `tests/test_retry.py`

- [ ] **Step 1: Write the test for async retry**

Create `tests/test_retry.py`:

```python
"""Tests for the async retry helper."""
import pytest
import anthropic
from unittest.mock import MagicMock, AsyncMock, patch

from scorched.retry import claude_call_with_retry


class TestClaudeCallWithRetry:
    @pytest.mark.asyncio
    async def test_returns_on_first_success(self):
        client = MagicMock()
        response = MagicMock()
        client.copy.return_value = client
        client.messages.create.return_value = response

        result = await claude_call_with_retry(client, "test", model="test-model", max_tokens=100)

        assert result is response
        assert client.messages.create.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_api_error(self):
        client = MagicMock()
        client.copy.return_value = client

        error_response = MagicMock()
        error_response.status_code = 529
        error = anthropic.APIStatusError(
            message="overloaded", response=error_response, body=None
        )

        success_response = MagicMock()
        client.messages.create.side_effect = [error, success_response]

        with patch("scorched.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await claude_call_with_retry(client, "test", model="m", max_tokens=1)

        assert result is success_response
        assert client.messages.create.call_count == 2
        mock_sleep.assert_called_once_with(1)  # first retry delay

    @pytest.mark.asyncio
    async def test_raises_after_all_retries_exhausted(self):
        client = MagicMock()
        client.copy.return_value = client

        error_response = MagicMock()
        error_response.status_code = 500
        error = anthropic.APIStatusError(
            message="server error", response=error_response, body=None
        )
        client.messages.create.side_effect = error

        with patch("scorched.retry.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(anthropic.APIStatusError):
                await claude_call_with_retry(client, "test", model="m", max_tokens=1)

        # 1 initial + 4 retries = 5 total attempts
        assert client.messages.create.call_count == 5

    @pytest.mark.asyncio
    async def test_uses_escalating_delays(self):
        client = MagicMock()
        client.copy.return_value = client

        error_response = MagicMock()
        error_response.status_code = 529
        error = anthropic.APIStatusError(
            message="overloaded", response=error_response, body=None
        )

        success_response = MagicMock()
        # Fail 3 times, succeed on 4th
        client.messages.create.side_effect = [error, error, error, success_response]

        with patch("scorched.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await claude_call_with_retry(client, "test", model="m", max_tokens=1)

        assert mock_sleep.call_args_list == [
            ((1,),),   # 1st retry
            ((5,),),   # 2nd retry
            ((30,),),  # 3rd retry
        ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_retry.py -v`
Expected: FAIL — `claude_call_with_retry` is not async yet, so `await` fails

- [ ] **Step 3: Convert retry.py to async**

Replace the full content of `src/scorched/retry.py`:

```python
"""Shared retry helper for Anthropic API calls."""
import asyncio
import logging

import anthropic

logger = logging.getLogger(__name__)

RETRY_DELAYS = [1, 5, 30, 60]  # seconds between retries


async def claude_call_with_retry(client: anthropic.Anthropic, label: str, **kwargs):
    """Call client.messages.create with escalating retry delays on API errors."""
    # Disable the SDK's own retries — we handle them with custom delays
    client = client.copy(max_retries=0)
    last_err = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return client.messages.create(**kwargs)
        except anthropic.APIStatusError as e:
            last_err = e
            if attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                logger.warning(
                    "%s failed (attempt %d/%d, status %s) — retrying in %ds",
                    label, attempt + 1, len(RETRY_DELAYS) + 1, e.status_code, delay,
                )
                await asyncio.sleep(delay)
            else:
                raise last_err
```

- [ ] **Step 4: Update claude_client.py call wrappers to await retry**

In `src/scorched/services/claude_client.py`, make `call_analysis`, `call_decision`, `call_risk_review`, and `call_playbook_update` async since they now call an async retry function:

Replace the import:
```python
from ..retry import claude_call_with_retry
```

Make each function that uses `claude_call_with_retry` async and add `await`:

```python
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


async def call_playbook_update(user_content: str):
    """Playbook update (uses claude-opus-4-6, not sonnet).

    Returns (response, updated_text).
    Raises anthropic.APIStatusError on failure after retries.
    """
    system_prompt = load_prompt("playbook_update")
    response = await claude_call_with_retry(
        _client(), "Playbook update",
        model="claude-opus-4-6",
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    return response, response.content[0].text.strip()
```

The remaining functions (`call_position_review`, `call_eod_review`, `call_intraday_exit`) stay sync for now — Task 3 will wrap them in retry.

- [ ] **Step 5: Update recommender.py to await the now-async calls**

In `src/scorched/services/recommender.py`, find every call to `call_analysis`, `call_decision`, and `call_risk_review` and add `await`:

Search for these patterns and add `await` before each:
- `call_analysis(` → `await call_analysis(`
- `call_decision(` → `await call_decision(`
- `call_risk_review(` → `await call_risk_review(`

These are already inside `async def generate_recommendations()`, so adding `await` is the only change needed.

Also in `src/scorched/services/eod_review.py`, find the call to `call_playbook_update` (if present) and add `await`. Check: the import is `from .claude_client import ... call_playbook_update` — if used, await it.

- [ ] **Step 6: Run all tests**

Run: `python3 -m pytest tests/ -v`
Expected: ALL PASS (including the new retry tests)

- [ ] **Step 7: Commit**

```bash
git add src/scorched/retry.py src/scorched/services/claude_client.py src/scorched/services/recommender.py src/scorched/services/eod_review.py tests/test_retry.py
git commit -m "fix: convert retry helper from blocking time.sleep to async asyncio.sleep

The retry helper was using time.sleep() which blocks the entire event loop
for up to 60 seconds during retries. Now uses asyncio.sleep() and the
call wrappers are properly async throughout the chain."
```

---

### Task 3: Consistent Retry Wrapping for All Claude Calls

`call_position_review`, `call_eod_review`, and `call_intraday_exit` in `claude_client.py` call `client.messages.create` directly without retry protection. If the API returns a 529 (overloaded) or 500 at 4 PM during Phase 3, the entire EOD review fails silently. Fix: wrap them in `claude_call_with_retry` like Calls 1-3.

**Files:**
- Modify: `src/scorched/services/claude_client.py:145-214`
- Modify: `src/scorched/services/eod_review.py` (await now-async calls)
- Modify: `src/scorched/api/intraday.py` (await now-async call_intraday_exit)
- Modify: `tests/test_claude_client.py` (add test for retry wrapping)

- [ ] **Step 1: Write test verifying retry is used on all call wrappers**

Add to `tests/test_claude_client.py`:

```python
from unittest.mock import patch, AsyncMock


class TestCallWrappersUseRetry:
    """All call_* wrappers should use claude_call_with_retry, not direct client.messages.create."""

    @pytest.mark.asyncio
    @patch("scorched.services.claude_client.claude_call_with_retry", new_callable=AsyncMock)
    @patch("scorched.services.claude_client._client")
    async def test_call_position_review_uses_retry(self, mock_client, mock_retry):
        mock_response = MagicMock()
        mock_response.content = [_text_block("result")]
        mock_retry.return_value = mock_response

        response, text = await call_position_review("test prompt")

        mock_retry.assert_called_once()
        assert text == "result"

    @pytest.mark.asyncio
    @patch("scorched.services.claude_client.claude_call_with_retry", new_callable=AsyncMock)
    @patch("scorched.services.claude_client._client")
    async def test_call_eod_review_uses_retry(self, mock_client, mock_retry):
        mock_response = MagicMock()
        mock_response.content = [_text_block("  updated playbook  ")]
        mock_retry.return_value = mock_response

        response, text = await call_eod_review("test prompt")

        mock_retry.assert_called_once()
        assert text == "updated playbook"  # stripped

    @pytest.mark.asyncio
    @patch("scorched.services.claude_client.claude_call_with_retry", new_callable=AsyncMock)
    @patch("scorched.services.claude_client._client")
    async def test_call_intraday_exit_uses_retry(self, mock_client, mock_retry):
        mock_response = MagicMock()
        mock_response.content = [_text_block('{"action": "hold"}')]
        mock_retry.return_value = mock_response

        response, text = await call_intraday_exit("test prompt")

        mock_retry.assert_called_once()
        assert text == '{"action": "hold"}'
```

Also add `import pytest` and update the imports at the top of the file:

```python
from scorched.services.claude_client import (
    extract_text, extract_thinking, parse_json_response,
    call_position_review, call_eod_review, call_intraday_exit,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_claude_client.py::TestCallWrappersUseRetry -v`
Expected: FAIL — these functions don't use retry yet and aren't async

- [ ] **Step 3: Wrap the three remaining call functions in retry**

In `src/scorched/services/claude_client.py`, replace `call_position_review`, `call_eod_review`, and `call_intraday_exit`:

```python
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


async def call_eod_review(user_content: str):
    """EOD review: distill learnings and update the playbook.

    Returns (response, updated_text).
    """
    system_prompt = load_prompt("eod_review")
    response = await claude_call_with_retry(
        _client(), "EOD review",
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
        model=MODEL,
        max_tokens=512,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    return response, response.content[0].text
```

- [ ] **Step 4: Update callers to await the now-async functions**

In `src/scorched/services/eod_review.py`, find:
```python
response, updated_content = _call_eod_review(user_content)
```
Replace with:
```python
response, updated_content = await _call_eod_review(user_content)
```

Find:
```python
pos_response, pos_text = _call_position_review(pos_prompt)
```
Replace with:
```python
pos_response, pos_text = await _call_position_review(pos_prompt)
```

In `src/scorched/api/intraday.py`, find:
```python
response, raw_text = call_intraday_exit(prompt)
```
Replace with:
```python
response, raw_text = await call_intraday_exit(prompt)
```

- [ ] **Step 5: Run all tests**

Run: `python3 -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add src/scorched/services/claude_client.py src/scorched/services/eod_review.py src/scorched/api/intraday.py tests/test_claude_client.py
git commit -m "fix: wrap all Claude calls in retry helper for consistent resilience

call_position_review, call_eod_review, and call_intraday_exit now use
claude_call_with_retry instead of calling client.messages.create directly.
All six Claude call wrappers now have consistent retry behavior."
```

---

## Phase 2: Quality Improvements

### Task 4: Context Size Measurement and Warning

The research context packet in `recommender.py` can grow unbounded on busy days. Add measurement logging and a warning threshold. No truncation — just observability.

**Files:**
- Modify: `src/scorched/services/recommender.py`

- [ ] **Step 1: Find where the context packet is assembled**

In `src/scorched/services/recommender.py`, locate where `call1_user` (or the user content for Call 1) is built. It will be a large string assembled from research data that gets passed to `call_analysis`.

- [ ] **Step 2: Add context size logging**

After the user content string is assembled but before it's passed to `call_analysis`, add:

```python
# Log context size — warn if approaching model limits
context_chars = len(call1_user)
context_est_tokens = context_chars // 4  # rough estimate: ~4 chars per token
logger.info("Call 1 context size: %d chars (~%dk tokens)", context_chars, context_est_tokens // 1000)
if context_est_tokens > 80_000:
    logger.warning(
        "Call 1 context is very large (%dk est. tokens) — risk of hitting context window limit. "
        "Consider reducing watchlist size or screener scope.",
        context_est_tokens // 1000,
    )
```

- [ ] **Step 3: Run existing tests to verify no regressions**

Run: `python3 -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add src/scorched/services/recommender.py
git commit -m "feat: add context size logging with warning for large research packets

Logs the character count and estimated token count of the Call 1 context
packet. Warns when estimated tokens exceed 80K to prevent opaque API
failures from hitting the context window limit."
```

---

### Task 5: Delete dashboard.html.bak and gitignore *.bak

**Files:**
- Delete: `src/scorched/static/dashboard.html.bak`
- Modify: `.gitignore`

- [ ] **Step 1: Delete the bak file and update gitignore**

```bash
git rm src/scorched/static/dashboard.html.bak
```

Add `*.bak` to `.gitignore` (check if `.gitignore` exists first, append to it):

```
# Backup files
*.bak
```

- [ ] **Step 2: Commit**

```bash
git add .gitignore
git commit -m "chore: remove dashboard.html.bak and gitignore *.bak files"
```

---

### Task 6: Structure the EOD Review Prompt

The `eod_review.md` prompt is the least structured of all prompts. Adding a clear outcome taxonomy will produce better playbook evolution over time. This is the prompt that drives compounding learning.

**Files:**
- Modify: `src/scorched/prompts/eod_review.md`

- [ ] **Step 1: Read the current prompt**

Read `src/scorched/prompts/eod_review.md` to understand the current structure.

- [ ] **Step 2: Rewrite with structured taxonomy**

Replace `src/scorched/prompts/eod_review.md` with:

```markdown
You are reviewing today's trading outcomes to update the playbook — a living strategy document that carries lessons forward to future decisions.

## Your Task

For each recommendation made today, classify the outcome into ONE of these categories:

1. **Thesis correct, trade worked** — the reasoning was sound AND the position moved favorably
2. **Thesis correct, trade failed on timing/execution** — the reasoning was sound but entry timing, position sizing, or market conditions undermined it
3. **Thesis wrong** — the core reasoning was flawed (missed a signal, misread the data, ignored a risk)
4. **Thesis untestable today** — not enough time has passed to evaluate (just entered, or market closed early)

For rejected recommendations (risk committee blocked), evaluate:
- Was the rejection correct? Did the stock move against the original thesis?
- Was it a missed opportunity? If so, what pattern should the risk committee recalibrate on?

## Playbook Update Rules

- **Keep what's working.** If a pattern produced good outcomes, reinforce it with specifics.
- **Kill what isn't.** If a pattern has failed 2+ times, flag it explicitly as deprecated with the reason.
- **Be concrete.** "Tech stocks are risky" is useless. "Buying semiconductor stocks into earnings week has failed 3/3 times — avoid" is actionable.
- **Track streaks.** Note winning and losing streaks. If the bot is on a losing streak, the playbook should reflect increased caution.
- **Date your lessons.** Include when a lesson was learned so future reviews can assess whether it's still relevant.

## Output

Produce the full updated playbook text. Do not summarize — write the complete replacement document. Preserve any lessons from the existing playbook that are still relevant, and add today's learnings.
```

- [ ] **Step 3: Run full test suite to verify no regressions**

Run: `python3 -m pytest tests/ -v`
Expected: ALL PASS (prompt changes don't break any tests — they're injected at runtime)

- [ ] **Step 4: Commit**

```bash
git add src/scorched/prompts/eod_review.md
git commit -m "feat: add structured outcome taxonomy to EOD review prompt

Adds a 4-category classification system for trade outcomes (thesis
correct+worked, correct+failed on timing, wrong, untestable) and
explicit rules for playbook evolution. This should produce more
actionable and specific playbook updates over time."
```

---

## Verification

After all 6 tasks are complete:

```bash
python3 -m pytest tests/ -v
```

All tests should pass. Key behaviors to verify:

1. **Hard stop:** A position down >= 5% is auto-sold without Claude evaluation
2. **Async retry:** `time.sleep` no longer appears anywhere in `retry.py`
3. **Consistent retry:** All 6 `call_*` functions in `claude_client.py` use `claude_call_with_retry`
4. **Context logging:** Call 1 context size is logged on every recommendation run
5. **No bak files:** `dashboard.html.bak` is gone, `*.bak` is in `.gitignore`
6. **EOD prompt:** `eod_review.md` has structured outcome categories
