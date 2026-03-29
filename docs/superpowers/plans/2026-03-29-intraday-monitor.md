# Intraday Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a lightweight intraday position monitor that checks held positions every 5 minutes during market hours, fires pure-function triggers on dangerous price action, and only calls Claude when a trigger trips — auto-executing sells if Claude confirms.

**Architecture:** Five pure trigger functions in `intraday.py` (no I/O, testable). A FastAPI endpoint that receives triggered positions, calls Claude, and executes sells via the broker. A cron poller script that fetches prices, runs triggers, and calls the endpoint. All thresholds configurable in `strategy.json`.

**Tech Stack:** Python 3.11, FastAPI, yfinance, Anthropic SDK (claude-sonnet-4-6), pytest

---

## File Structure

### Files to Create
- `src/scorched/intraday.py` — Pure trigger check functions (no I/O)
- `src/scorched/api/intraday.py` — POST /api/v1/intraday/evaluate endpoint
- `src/scorched/prompts/intraday_exit.md` — Claude prompt for exit decisions
- `cron/intraday_monitor.py` — Cron poller script
- `tests/test_intraday.py` — Tests for trigger functions

### Files to Modify
- `src/scorched/services/claude_client.py` — Add `call_intraday_exit()`
- `src/scorched/main.py` — Mount intraday router
- `strategy.json` — Add `intraday_monitor` config block

---

### Task 1: Pure Trigger Functions

**Files:**
- Create: `src/scorched/intraday.py`
- Create: `tests/test_intraday.py`

- [ ] **Step 1: Write failing tests for all 5 triggers**

Create `tests/test_intraday.py`:

```python
"""Tests for intraday trigger checks."""
from decimal import Decimal
import pytest
from scorched.intraday import (
    check_position_drop_from_entry,
    check_position_drop_from_open,
    check_spy_intraday_drop,
    check_vix_level,
    check_volume_surge,
    check_intraday_triggers,
    check_market_triggers,
)


class TestPositionDropFromEntry:
    def test_fires_when_drop_exceeds_threshold(self):
        result = check_position_drop_from_entry(
            current_price=Decimal("94"), entry_price=Decimal("100"), threshold_pct=5.0
        )
        assert not result.passed
        assert "6.0%" in result.reason

    def test_passes_when_drop_below_threshold(self):
        result = check_position_drop_from_entry(
            current_price=Decimal("96"), entry_price=Decimal("100"), threshold_pct=5.0
        )
        assert result.passed

    def test_passes_when_price_is_up(self):
        result = check_position_drop_from_entry(
            current_price=Decimal("105"), entry_price=Decimal("100"), threshold_pct=5.0
        )
        assert result.passed

    def test_handles_zero_entry(self):
        result = check_position_drop_from_entry(
            current_price=Decimal("50"), entry_price=Decimal("0"), threshold_pct=5.0
        )
        assert result.passed


class TestPositionDropFromOpen:
    def test_fires_when_drop_exceeds_threshold(self):
        result = check_position_drop_from_open(
            current_price=Decimal("96.5"), today_open=Decimal("100"), threshold_pct=3.0
        )
        assert not result.passed
        assert "3.5%" in result.reason

    def test_passes_when_drop_below_threshold(self):
        result = check_position_drop_from_open(
            current_price=Decimal("98"), today_open=Decimal("100"), threshold_pct=3.0
        )
        assert result.passed


class TestSpyIntradayDrop:
    def test_fires_when_spy_drops(self):
        result = check_spy_intraday_drop(
            spy_current=Decimal("490"), spy_open=Decimal("500"), threshold_pct=1.5
        )
        assert not result.passed
        assert "2.0%" in result.reason

    def test_passes_when_spy_stable(self):
        result = check_spy_intraday_drop(
            spy_current=Decimal("499"), spy_open=Decimal("500"), threshold_pct=1.5
        )
        assert result.passed


class TestVixLevel:
    def test_fires_when_vix_high(self):
        result = check_vix_level(vix_current=Decimal("35"), threshold=30.0)
        assert not result.passed
        assert "35" in result.reason

    def test_passes_when_vix_normal(self):
        result = check_vix_level(vix_current=Decimal("22"), threshold=30.0)
        assert result.passed


class TestVolumeSurge:
    def test_fires_on_volume_spike(self):
        result = check_volume_surge(
            current_volume=15_000_000, avg_volume_20d=4_000_000, threshold_multiplier=3.0
        )
        assert not result.passed
        assert "3.8x" in result.reason

    def test_passes_on_normal_volume(self):
        result = check_volume_surge(
            current_volume=5_000_000, avg_volume_20d=4_000_000, threshold_multiplier=3.0
        )
        assert result.passed

    def test_handles_zero_avg_volume(self):
        result = check_volume_surge(
            current_volume=5_000_000, avg_volume_20d=0, threshold_multiplier=3.0
        )
        assert result.passed


class TestCheckMarketTriggers:
    def test_returns_list_of_fired_triggers(self):
        results = check_market_triggers(
            spy_current=Decimal("480"), spy_open=Decimal("500"),
            vix_current=Decimal("35"),
            config={"spy_intraday_drop_pct": 2.0, "vix_absolute_max": 30},
        )
        assert len(results) == 2  # both SPY and VIX fire

    def test_returns_empty_when_all_pass(self):
        results = check_market_triggers(
            spy_current=Decimal("499"), spy_open=Decimal("500"),
            vix_current=Decimal("18"),
            config={"spy_intraday_drop_pct": 2.0, "vix_absolute_max": 30},
        )
        assert len(results) == 0


class TestCheckIntradayTriggers:
    def test_combines_position_and_market_triggers(self):
        market_triggers = []  # no market triggers
        results = check_intraday_triggers(
            current_price=Decimal("90"), entry_price=Decimal("100"),
            today_open=Decimal("99"), current_volume=20_000_000,
            avg_volume_20d=5_000_000, market_triggers=market_triggers,
            config={
                "position_drop_from_entry_pct": 5.0,
                "position_drop_from_open_pct": 3.0,
                "volume_surge_multiplier": 3.0,
            },
        )
        # Entry drop (10%), open drop (9.1%), volume surge (4x) — all 3 fire
        assert len(results) == 3

    def test_includes_market_triggers(self):
        from scorched.circuit_breaker import GateResult
        market_triggers = [GateResult(passed=False, reason="SPY down 3%")]
        results = check_intraday_triggers(
            current_price=Decimal("99"), entry_price=Decimal("100"),
            today_open=Decimal("100"), current_volume=1_000_000,
            avg_volume_20d=1_000_000, market_triggers=market_triggers,
            config={
                "position_drop_from_entry_pct": 5.0,
                "position_drop_from_open_pct": 3.0,
                "volume_surge_multiplier": 3.0,
            },
        )
        assert len(results) == 1
        assert "SPY" in results[0].reason

    def test_empty_when_nothing_triggers(self):
        results = check_intraday_triggers(
            current_price=Decimal("101"), entry_price=Decimal("100"),
            today_open=Decimal("100.5"), current_volume=1_000_000,
            avg_volume_20d=1_000_000, market_triggers=[],
            config={
                "position_drop_from_entry_pct": 5.0,
                "position_drop_from_open_pct": 3.0,
                "volume_surge_multiplier": 3.0,
            },
        )
        assert len(results) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/test_intraday.py -v`
Expected: FAIL — `scorched.intraday` module doesn't exist

- [ ] **Step 3: Implement the trigger functions**

Create `src/scorched/intraday.py`:

```python
"""Intraday position monitoring — pure trigger check functions.

All functions are pure (no I/O) and return GateResult from circuit_breaker.
"""
from decimal import Decimal

from .circuit_breaker import GateResult


def check_position_drop_from_entry(
    current_price: Decimal,
    entry_price: Decimal,
    threshold_pct: float,
) -> GateResult:
    """Fire if position has dropped > threshold_pct from entry price."""
    if entry_price <= 0:
        return GateResult(passed=True)
    drop_pct = float((entry_price - current_price) / entry_price * 100)
    if drop_pct > threshold_pct:
        return GateResult(
            passed=False,
            reason=f"Down {drop_pct:.1f}% from entry ${entry_price} (threshold: {threshold_pct}%)",
        )
    return GateResult(passed=True)


def check_position_drop_from_open(
    current_price: Decimal,
    today_open: Decimal,
    threshold_pct: float,
) -> GateResult:
    """Fire if position has dropped > threshold_pct from today's open."""
    if today_open <= 0:
        return GateResult(passed=True)
    drop_pct = float((today_open - current_price) / today_open * 100)
    if drop_pct > threshold_pct:
        return GateResult(
            passed=False,
            reason=f"Down {drop_pct:.1f}% from today's open ${today_open} (threshold: {threshold_pct}%)",
        )
    return GateResult(passed=True)


def check_spy_intraday_drop(
    spy_current: Decimal,
    spy_open: Decimal,
    threshold_pct: float,
) -> GateResult:
    """Fire if SPY has dropped > threshold_pct intraday."""
    if spy_open <= 0:
        return GateResult(passed=True)
    drop_pct = float((spy_open - spy_current) / spy_open * 100)
    if drop_pct > threshold_pct:
        return GateResult(
            passed=False,
            reason=f"SPY down {drop_pct:.1f}% intraday (threshold: {threshold_pct}%)",
        )
    return GateResult(passed=True)


def check_vix_level(
    vix_current: Decimal,
    threshold: float,
) -> GateResult:
    """Fire if VIX exceeds absolute threshold."""
    if float(vix_current) > threshold:
        return GateResult(
            passed=False,
            reason=f"VIX at {float(vix_current):.1f} exceeds {threshold}",
        )
    return GateResult(passed=True)


def check_volume_surge(
    current_volume: float,
    avg_volume_20d: float,
    threshold_multiplier: float,
) -> GateResult:
    """Fire if today's volume exceeds avg * threshold."""
    if avg_volume_20d <= 0:
        return GateResult(passed=True)
    ratio = current_volume / avg_volume_20d
    if ratio > threshold_multiplier:
        return GateResult(
            passed=False,
            reason=f"Volume surge {ratio:.1f}x average (threshold: {threshold_multiplier}x)",
        )
    return GateResult(passed=True)


def check_market_triggers(
    spy_current: Decimal,
    spy_open: Decimal,
    vix_current: Decimal,
    config: dict,
) -> list[GateResult]:
    """Run market-level triggers. Returns list of fired GateResults (empty = all clear)."""
    fired = []
    spy_result = check_spy_intraday_drop(
        spy_current, spy_open, config.get("spy_intraday_drop_pct", 2.0)
    )
    if not spy_result.passed:
        fired.append(spy_result)
    vix_result = check_vix_level(vix_current, config.get("vix_absolute_max", 30))
    if not vix_result.passed:
        fired.append(vix_result)
    return fired


def check_intraday_triggers(
    current_price: Decimal,
    entry_price: Decimal,
    today_open: Decimal,
    current_volume: float,
    avg_volume_20d: float,
    market_triggers: list[GateResult],
    config: dict,
) -> list[GateResult]:
    """Run all triggers for a single position. Returns list of fired GateResults."""
    fired = list(market_triggers)  # include any market-level triggers

    entry_result = check_position_drop_from_entry(
        current_price, entry_price, config.get("position_drop_from_entry_pct", 5.0)
    )
    if not entry_result.passed:
        fired.append(entry_result)

    open_result = check_position_drop_from_open(
        current_price, today_open, config.get("position_drop_from_open_pct", 3.0)
    )
    if not open_result.passed:
        fired.append(open_result)

    vol_result = check_volume_surge(
        current_volume, avg_volume_20d, config.get("volume_surge_multiplier", 3.0)
    )
    if not vol_result.passed:
        fired.append(vol_result)

    return fired
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/test_intraday.py -v`
Expected: All PASS

- [ ] **Step 5: Run all tests**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/ -v`
Expected: All PASS (61 existing + new intraday tests)

- [ ] **Step 6: Commit**

```bash
git add src/scorched/intraday.py tests/test_intraday.py
git commit -m "feat: add pure intraday trigger check functions

Five configurable triggers: position drop from entry, drop from open,
SPY intraday drop, VIX level, and volume surge. All pure functions
reusing GateResult from circuit_breaker. Full test coverage.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Claude Prompt and Client Function

**Files:**
- Create: `src/scorched/prompts/intraday_exit.md`
- Modify: `src/scorched/services/claude_client.py`

- [ ] **Step 1: Create the intraday exit prompt**

Create `src/scorched/prompts/intraday_exit.md`:

```markdown
You are reviewing a position that has triggered an intraday alert. Your job is to decide whether to exit immediately, exit partially, or hold.

Consider:
- How severe is the trigger? A -5.1% drop (barely over threshold) is different from -8%.
- Is this stock-specific or a broad market move? If SPY is down similarly, it may be systemic, not thesis-breaking.
- How strong was the original thesis? Is the catalyst still valid despite the price drop?
- How many days held? A day-1 drop may mean bad entry timing; a day-7 drop after gains may mean the trade is done.

Be decisive. If the thesis is broken or the loss is accelerating, exit. If this is normal volatility within the thesis timeframe, hold. Don't hedge — pick one.

Respond with valid JSON only:
{
  "action": "exit_full" or "exit_partial" or "hold",
  "partial_pct": null or 50,
  "reasoning": "1-2 sentences explaining the decision"
}
```

- [ ] **Step 2: Add call_intraday_exit to claude_client.py**

Add to the end of `src/scorched/services/claude_client.py`, before the closing of the file:

```python
def call_intraday_exit(user_content: str):
    """Intraday exit evaluation — small focused call.

    Returns (response, raw_text).
    """
    system_prompt = load_prompt("intraday_exit")
    client = _client()

    logger.info("Intraday exit evaluation call")
    response = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    return response, response.content[0].text
```

- [ ] **Step 3: Verify prompt loads**

Run: `cd /home/ubuntu/tradebot && python3 -c "from scorched.prompts import load_prompt; p = load_prompt('intraday_exit'); print(len(p), 'chars'); print(p[:80])"`
Expected: Prints char count and first line of prompt

- [ ] **Step 4: Run all tests**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/scorched/prompts/intraday_exit.md src/scorched/services/claude_client.py
git commit -m "feat: add intraday exit prompt and Claude client function

Focused prompt for exit decisions only — no market analysis. Uses Sonnet
with max_tokens=512 for fast, cheap calls (~$0.01 per invocation).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: API Endpoint

**Files:**
- Create: `src/scorched/api/intraday.py`
- Modify: `src/scorched/main.py`
- Modify: `src/scorched/schemas.py`

- [ ] **Step 1: Add schemas to schemas.py**

Add to the end of `src/scorched/schemas.py`:

```python
# ── Intraday Monitor ──────────────────────────────────────────────────────────

class IntradayTriggerItem(BaseModel):
    symbol: str
    trigger_reasons: list[str]
    current_price: Decimal
    entry_price: Decimal
    today_open: Decimal
    today_high: Decimal
    today_low: Decimal
    days_held: int
    shares: Decimal
    original_reasoning: str = ""


class IntradayMarketContext(BaseModel):
    spy_change_pct: float = 0.0
    vix_current: float = 0.0


class IntradayEvaluateRequest(BaseModel):
    triggers: list[IntradayTriggerItem]
    market_context: IntradayMarketContext = IntradayMarketContext()


class IntradayDecision(BaseModel):
    symbol: str
    action: str  # 'exit_full' | 'exit_partial' | 'hold'
    reasoning: str
    trade_result: dict | None = None  # present if a sell was executed


class IntradayEvaluateResponse(BaseModel):
    decisions: list[IntradayDecision]
```

- [ ] **Step 2: Create the intraday API endpoint**

Create `src/scorched/api/intraday.py`:

```python
"""Intraday monitoring endpoint — evaluates triggered positions via Claude."""
import json
import logging
from decimal import Decimal

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


def _build_exit_prompt(trigger, market_ctx) -> str:
    """Build the user prompt for a single triggered position."""
    lines = [
        f"Position: {trigger.symbol}, {trigger.shares} shares, "
        f"entry ${trigger.entry_price}, current ${trigger.current_price} "
        f"({float((trigger.current_price - trigger.entry_price) / trigger.entry_price * 100):+.1f}%)",
        f"Triggers fired:",
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


@router.post("/evaluate", response_model=IntradayEvaluateResponse)
async def evaluate_triggers(
    body: IntradayEvaluateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Evaluate triggered positions via Claude and execute exits."""
    decisions = []

    for trigger in body.triggers:
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
            [r for r in trigger.trigger_reasons],
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

- [ ] **Step 3: Mount the router in main.py**

In `src/scorched/main.py`, add the import and mount:

After the existing import line:
```python
from .api import broker_status, costs, market, onboarding, playbook, portfolio, recommendations, strategy, system, trades
```
Change to:
```python
from .api import broker_status, costs, intraday, market, onboarding, playbook, portfolio, recommendations, strategy, system, trades
```

After the existing router mounts, add:
```python
app.include_router(intraday.router, prefix="/api/v1")
```

- [ ] **Step 4: Run all tests**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/scorched/api/intraday.py src/scorched/main.py src/scorched/schemas.py
git commit -m "feat: add POST /api/v1/intraday/evaluate endpoint

Receives triggered positions, calls Claude for exit decisions, and
auto-executes sells via broker when Claude confirms. Records token
usage for cost tracking.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Strategy Config

**Files:**
- Modify: `strategy.json`

- [ ] **Step 1: Add intraday_monitor config to strategy.json**

Add the `intraday_monitor` key to `strategy.json`. Read the file first, then add the new key at the top level alongside existing keys:

```json
{
  "intraday_monitor": {
    "enabled": true,
    "position_drop_from_entry_pct": 5.0,
    "position_drop_from_open_pct": 3.0,
    "spy_intraday_drop_pct": 2.0,
    "vix_absolute_max": 30,
    "volume_surge_multiplier": 3.0,
    "cooldown_minutes": 30
  }
}
```

This should be added as a new key in the existing JSON object, not replace it.

- [ ] **Step 2: Commit**

```bash
git add strategy.json
git commit -m "feat: add intraday_monitor config to strategy.json

Five configurable trigger thresholds plus cooldown. Enabled by default.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Cron Poller Script

**Files:**
- Create: `cron/intraday_monitor.py`

- [ ] **Step 1: Create the poller script**

Create `cron/intraday_monitor.py`:

```python
#!/usr/bin/env python3
"""
Intraday Monitor — runs every 5 minutes during market hours.

Checks held positions against configurable triggers. Only calls Claude
(via POST /api/v1/intraday/evaluate) when a trigger fires. Zero LLM
cost on quiet days.

Cron: */5 13-19 * * 1-5  (script self-gates on ET market hours)
"""
import json
import os
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

# Add cron directory to path for common module
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_env, http_get, http_post, send_telegram, now_et

load_env()

# Add src/ to path for intraday trigger functions
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from scorched.intraday import check_intraday_triggers, check_market_triggers

COOLDOWN_FILE = "/tmp/intraday_cooldown.json"


def is_market_hours(now_est) -> bool:
    """Return True if within 9:35 AM - 3:55 PM ET."""
    t = now_est.time()
    from datetime import time as dt_time
    return dt_time(9, 35) <= t <= dt_time(15, 55)


def load_cooldowns() -> dict:
    """Load cooldown timestamps from file."""
    if not os.path.exists(COOLDOWN_FILE):
        return {}
    try:
        with open(COOLDOWN_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_cooldowns(cooldowns: dict) -> None:
    with open(COOLDOWN_FILE, "w") as f:
        json.dump(cooldowns, f)


def is_on_cooldown(symbol: str, cooldowns: dict, cooldown_minutes: int) -> bool:
    last_trigger = cooldowns.get(symbol, 0)
    return (time.time() - last_trigger) < cooldown_minutes * 60


def fetch_position_data(symbols: list[str]) -> dict:
    """Fetch current prices, today's OHLV, and 20d avg volume via yfinance."""
    import yfinance as yf

    data = {}
    all_symbols = list(set(symbols + ["SPY", "^VIX"]))

    for sym in all_symbols:
        try:
            ticker = yf.Ticker(sym)
            # 1-day intraday for today's OHLCV
            hist_1d = ticker.history(period="1d")
            # 1-month daily for avg volume
            hist_1mo = ticker.history(period="1mo")

            if hist_1d.empty:
                continue

            current_price = float(hist_1d["Close"].iloc[-1])
            today_open = float(hist_1d["Open"].iloc[-1])
            today_high = float(hist_1d["High"].iloc[-1])
            today_low = float(hist_1d["Low"].iloc[-1])
            today_volume = float(hist_1d["Volume"].iloc[-1])

            avg_volume_20d = 0.0
            if not hist_1mo.empty and len(hist_1mo) >= 2:
                # Exclude today from average
                avg_volume_20d = float(hist_1mo["Volume"].iloc[:-1].tail(20).mean())

            data[sym] = {
                "current_price": current_price,
                "today_open": today_open,
                "today_high": today_high,
                "today_low": today_low,
                "today_volume": today_volume,
                "avg_volume_20d": avg_volume_20d,
            }
        except Exception as e:
            print(f"  Fetch failed for {sym}: {e}")

    return data


def main():
    now_est, today_str = now_et()

    if not is_market_hours(now_est):
        return  # silently exit outside market hours

    print(f"[{now_est.strftime('%H:%M:%S')}] Intraday monitor check")

    # Get held positions
    try:
        portfolio = http_get("/api/v1/portfolio")
    except Exception as e:
        print(f"  Portfolio fetch failed: {e}")
        return

    positions = portfolio.get("positions", [])
    if not positions:
        return  # no positions, nothing to monitor

    # Load strategy config
    strategy_path = Path(__file__).resolve().parent.parent / "strategy.json"
    try:
        strategy = json.loads(strategy_path.read_text())
    except (OSError, json.JSONDecodeError):
        strategy = {}

    config = strategy.get("intraday_monitor", {})
    if not config.get("enabled", True):
        return

    cooldown_minutes = config.get("cooldown_minutes", 30)
    cooldowns = load_cooldowns()

    # Batch fetch market data
    held_symbols = [p["symbol"] for p in positions]
    print(f"  Checking {len(held_symbols)} positions: {held_symbols}")
    data = fetch_position_data(held_symbols)

    if not data:
        print("  No market data available")
        return

    # Market-level triggers
    spy_data = data.get("SPY", {})
    vix_data = data.get("^VIX", {})
    market_triggers = check_market_triggers(
        spy_current=Decimal(str(spy_data.get("current_price", 0))),
        spy_open=Decimal(str(spy_data.get("today_open", 0))),
        vix_current=Decimal(str(vix_data.get("current_price", 0))),
        config=config,
    )

    if market_triggers:
        reasons = [t.reason for t in market_triggers]
        print(f"  Market triggers fired: {reasons}")

    # Per-position triggers
    triggered_positions = []
    for pos in positions:
        symbol = pos["symbol"]

        if is_on_cooldown(symbol, cooldowns, cooldown_minutes):
            continue

        sym_data = data.get(symbol)
        if not sym_data:
            continue

        triggers = check_intraday_triggers(
            current_price=Decimal(str(sym_data["current_price"])),
            entry_price=Decimal(str(pos["avg_cost_basis"])),
            today_open=Decimal(str(sym_data["today_open"])),
            current_volume=sym_data["today_volume"],
            avg_volume_20d=sym_data["avg_volume_20d"],
            market_triggers=market_triggers,
            config=config,
        )

        if triggers:
            days_held = pos.get("days_held", 0)
            triggered_positions.append({
                "symbol": symbol,
                "trigger_reasons": [t.reason for t in triggers],
                "current_price": sym_data["current_price"],
                "entry_price": float(pos["avg_cost_basis"]),
                "today_open": sym_data["today_open"],
                "today_high": sym_data["today_high"],
                "today_low": sym_data["today_low"],
                "days_held": days_held,
                "shares": float(pos["shares"]),
                "original_reasoning": "",  # not available from portfolio endpoint
            })
            cooldowns[symbol] = time.time()

    if not triggered_positions:
        print("  All clear — no triggers fired")
        return

    print(f"  TRIGGERS FIRED for {[t['symbol'] for t in triggered_positions]}")

    # Save cooldowns before the API call (in case it's slow)
    save_cooldowns(cooldowns)

    # Call the evaluate endpoint
    spy_change_pct = 0.0
    if spy_data.get("today_open") and spy_data.get("current_price"):
        spy_change_pct = (spy_data["current_price"] - spy_data["today_open"]) / spy_data["today_open"] * 100

    try:
        result = http_post("/api/v1/intraday/evaluate", {
            "triggers": triggered_positions,
            "market_context": {
                "spy_change_pct": round(spy_change_pct, 2),
                "vix_current": vix_data.get("current_price", 0),
            },
        }, timeout=120)
    except Exception as e:
        msg = f"INTRADAY ALERT - Trigger evaluation failed\nTriggered: {[t['symbol'] for t in triggered_positions]}\nError: {e}"
        send_telegram(msg)
        print(f"  Evaluate failed: {e}")
        return

    # Send Telegram for each decision
    for decision in result.get("decisions", []):
        symbol = decision["symbol"]
        action = decision["action"]
        reasoning = decision["reasoning"]

        if action in ("exit_full", "exit_partial"):
            trade = decision.get("trade_result") or {}
            shares = trade.get("shares", "?")
            price = trade.get("execution_price", "?")
            gain = trade.get("realized_gain", 0)
            gain_sign = "+" if gain >= 0 else ""
            msg = (
                f"INTRADAY EXIT: {symbol}\n"
                f"Sold {shares}sh @ ${price}\n"
                f"Realized: {gain_sign}${gain:,.2f}\n"
                f"Reason: {reasoning}"
            )
        else:
            # Claude said hold
            triggers = [t for t in triggered_positions if t["symbol"] == symbol]
            trigger_reasons = triggers[0]["trigger_reasons"] if triggers else []
            msg = (
                f"INTRADAY ALERT: {symbol} — HOLD\n"
                f"Triggers: {', '.join(trigger_reasons)}\n"
                f"Claude says: {reasoning}"
            )

        send_telegram(msg)
        print(f"  {symbol}: {action} — {reasoning[:80]}")

    print(f"  Intraday check complete: {len(result.get('decisions', []))} decisions")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the script runs (outside market hours it should exit silently)**

Run: `cd /home/ubuntu/tradebot && python3 cron/intraday_monitor.py`
Expected: Exits silently (it's outside market hours) or prints a check message if within hours.

- [ ] **Step 3: Run all tests**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add cron/intraday_monitor.py
git commit -m "feat: add intraday monitor cron poller

Checks held positions every 5 min during market hours via yfinance.
Pure trigger checks cost $0. Only escalates to Claude when thresholds
breach. Auto-executes sells and sends Telegram notifications.
Includes per-symbol cooldown to prevent rapid-fire triggers.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:**
- 5 trigger functions (entry drop, open drop, SPY, VIX, volume) — Task 1 ✓
- Claude prompt for exit decisions — Task 2 ✓
- `call_intraday_exit()` in claude_client — Task 2 ✓
- POST /api/v1/intraday/evaluate endpoint — Task 3 ✓
- Schemas for request/response — Task 3 ✓
- Router mounted in main.py — Task 3 ✓
- strategy.json config — Task 4 ✓
- Cron poller script — Task 5 ✓
- Market hours gating — Task 5 (is_market_hours) ✓
- Cooldown mechanism — Task 5 (load/save cooldowns) ✓
- Telegram notifications — Task 5 ✓
- Sells only / never buys — Task 3 (endpoint only does submit_sell) ✓
- Cost recording — Task 3 (record_usage call) ✓

**2. Placeholder scan:** No TBD/TODO found. All code blocks complete.

**3. Type consistency:** `GateResult` used consistently from `circuit_breaker`. `check_intraday_triggers` and `check_market_triggers` signatures match between Task 1 tests and implementation. `IntradayTriggerItem` fields match what the cron script sends. `call_intraday_exit` signature matches between Task 2 and Task 3 usage.
