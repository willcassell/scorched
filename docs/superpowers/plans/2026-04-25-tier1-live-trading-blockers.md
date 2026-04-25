# Tier 1 — Live-Trading Safety Blockers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every Critical and High-severity finding from the 2026-04-25 audit that blocks unattended live trading, by enforcing risk rules deterministically in code rather than via prompt compliance.

**Architecture:** Three-layer hardening. (1) `strategy.json` becomes the *single* numeric source of truth — every gate value comes from one canonical field. (2) Deterministic Python validators run *after* recommendation generation **and again** at trade-confirm time, immediately before broker submission. (3) `analyst_guidance.md` stays as a Claude-facing hint, but no longer gates execution. Auth is fail-closed in every mode that exposes mutations, and live trading requires two independent kill switches.

**Tech Stack:** FastAPI + SQLAlchemy async, Alpaca SDK (`alpaca-py`), pytest + pytest-asyncio, `Decimal` for money, in-memory SQLite for tests. Tests must export `ANTHROPIC_API_KEY=test` because settings load at import time.

**Key Decisions Locked In (D1–D6):**
- D1: Hard stop is **8%** (canonical field `intraday_monitor.hard_stop_pct`).
- D2: Trailing stops remain Claude-reviewed but every breach fires a Telegram alert regardless of Claude's verdict.
- D3: `/trades/confirm` is server-decides — quantity/price come from the stored recommendation; client `shares`/`execution_price` are advisory only and must match within tolerance.
- D4: Unknown sector fails closed; Finnhub is the fallback classifier.
- D5: `LIVE_TRADING_ENABLED=true` env var is required *in addition to* `BROKER_MODE=alpaca_live`.
- D6: 90-day decision-replay against frozen snapshots is a separate Tier 4 plan; this plan does not block on it.

**Test command convention:** `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest <path> -v`

**Commit convention:** Conventional commits (`feat:`, `fix:`, `refactor:`, `chore:`). Each task ends with one commit. Co-author trailer is added by Claude Code automatically; do not add manually.

**Working dir:** `/home/ubuntu/tradebot` — operate directly on this VM checkout, no worktree (per user's working-on-VM convention).

**Order of tasks:** 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10. Tasks 1–5 fix gate math/config; task 6 reuses those gates at confirm time and depends on 2–5; tasks 7–10 are independent of 6.

---

## File Structure

| File | Role | Tasks that touch it |
|---|---|---|
| `strategy.json` | Add canonical `hard_stop_pct: 8.0`; keep `position_drop_from_entry_pct: 5.0` as separate Claude-trigger threshold. | 1 |
| `src/scorched/api/intraday.py` | Read `hard_stop_pct` for auto-exit; add emergency sell buffer; idempotency-keyed retry on hard-stop sell failure. | 1, 7 |
| `src/scorched/services/guidance_lint.py` | Replace info-only stop-loss check with hard error when `hard_stop_pct != 8`. | 1 |
| `src/scorched/services/recommender.py` | Fix cash-floor formula, make holdings/position/cash gates cumulative across same-session buys, fail-closed sector gate. | 2, 3, 4, 5 |
| `src/scorched/risk_gates.py` (NEW) | Pure functions for cash/holdings/position/sector/drawdown re-checks, callable from both `recommender.py` and `api/trades.py`. | 2, 3, 4, 5, 6 |
| `src/scorched/api/trades.py` | `/trades/confirm` becomes server-decides — read stored rec, fetch live price, run all gates, then submit. | 6 |
| `src/scorched/schemas.py` | `ConfirmTradeRequest.shares` and `execution_price` become Optional advisories. | 6 |
| `cron/tradebot_phase2.py` | Update payload — only `recommendation_id` is required. | 6 |
| `src/scorched/services/finnhub_data.py` | Add `fetch_sector_for_symbol()` Finnhub fallback. | 5 |
| `src/scorched/circuit_breaker.py` | Replace yfinance fetch with Alpaca snapshots; wire `check_gap_up_gate` into `run_circuit_breaker`. | 8 |
| `src/scorched/main.py` + `src/scorched/config.py` + `src/scorched/api/deps.py` | Fail startup without PIN in any mutation-enabled mode; require `LIVE_TRADING_ENABLED` for live mode; auth read endpoints. | 9, 10 |
| `src/scorched/api/onboarding.py` | One-shot bootstrap token; reject onboarding routes after first successful save. | 9 |
| `src/scorched/api/{recommendations,system,portfolio,playbook,broker_status}.py` | Add `Depends(require_owner_pin)` to GET endpoints. | 9 |
| `tests/test_*.py` | New tests per task. | every task |

---

## Task 1 — Canonical hard-stop field (8%)

**Why first:** Cheapest, no dependencies, unblocks reasoning about stop semantics in every later task.

**Files:**
- Modify: `strategy.json` (line 47 — change `"hard_stop_pct": 5.0` to `"hard_stop_pct": 8.0`; keep `position_drop_from_entry_pct: 5.0` as the separate Claude-trigger threshold)
- Modify: `src/scorched/api/intraday.py:32-39` (`_load_hard_stop_pct` reads `hard_stop_pct`, default 8.0)
- Modify: `src/scorched/services/guidance_lint.py:68-91` (raise error when `hard_stop_pct != 8`)
- Modify: `analyst_guidance.md` — verify rule #5 already says "-8%" (no change expected; lint will catch it if not)
- Test: `tests/test_intraday_endpoint.py` (new test) and `tests/test_guidance_lint.py` (new file)

- [ ] **Step 1.1: Write the failing test for `_load_hard_stop_pct`**

Add to `tests/test_intraday_endpoint.py` (create the file if missing — check first):

```python
import json
from pathlib import Path
from unittest.mock import patch, mock_open

from scorched.api.intraday import _load_hard_stop_pct


class TestLoadHardStopPct:
    def test_reads_hard_stop_pct_field(self):
        strategy = {"intraday_monitor": {"hard_stop_pct": 8.0, "position_drop_from_entry_pct": 5.0}}
        with patch("builtins.open", mock_open(read_data=json.dumps(strategy))):
            assert _load_hard_stop_pct() == 8.0

    def test_does_not_use_position_drop_field(self):
        # Regression: previous bug used position_drop_from_entry_pct instead.
        strategy = {"intraday_monitor": {"hard_stop_pct": 8.0, "position_drop_from_entry_pct": 5.0}}
        with patch("builtins.open", mock_open(read_data=json.dumps(strategy))):
            value = _load_hard_stop_pct()
            assert value == 8.0
            assert value != 5.0

    def test_default_when_field_missing(self):
        strategy = {"intraday_monitor": {}}
        with patch("builtins.open", mock_open(read_data=json.dumps(strategy))):
            assert _load_hard_stop_pct() == 8.0

    def test_default_when_file_missing(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            assert _load_hard_stop_pct() == 8.0
```

- [ ] **Step 1.2: Run the test to verify it fails**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_intraday_endpoint.py::TestLoadHardStopPct -v`
Expected: All four tests FAIL — `_load_hard_stop_pct` currently reads `position_drop_from_entry_pct` and returns 5.0.

- [ ] **Step 1.3: Update `_load_hard_stop_pct`**

In `src/scorched/api/intraday.py`, replace lines 32–39 with:

```python
def _load_hard_stop_pct() -> float:
    """Read hard stop threshold from strategy.json (default 8.0%).

    This is the deterministic auto-exit threshold (rule #5: -8% from entry).
    It is intentionally separate from `position_drop_from_entry_pct`, which
    is the looser Claude-evaluation trigger (default 5%).
    """
    try:
        with open(STRATEGY_PATH) as f:
            data = json.load(f)
        return float(data.get("intraday_monitor", {}).get("hard_stop_pct", 8.0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return 8.0
```

- [ ] **Step 1.4: Set canonical value in `strategy.json`**

In `strategy.json`, change line 47 from `"hard_stop_pct": 5.0` to `"hard_stop_pct": 8.0`. Leave `position_drop_from_entry_pct: 5.0` untouched on line 43.

- [ ] **Step 1.5: Run the test to verify it passes**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_intraday_endpoint.py::TestLoadHardStopPct -v`
Expected: 4 passed.

- [ ] **Step 1.6: Update guidance_lint to enforce 8%**

Replace `_check_stop_loss` in `src/scorched/services/guidance_lint.py` (lines 68–91) with:

```python
def _check_stop_loss(strategy: dict, guidance: str) -> Finding:
    """Verify hard stop is 8% in both strategy.json and analyst_guidance.md rule #5."""
    strat_val = (strategy.get("intraday_monitor") or {}).get("hard_stop_pct")
    guide_val = _find_first(
        r"Stop loss at\s*-?(\d+(?:\.\d+)?)\s*%\s*from entry", guidance, re.IGNORECASE,
    )
    if strat_val is None or guide_val is None:
        return Finding(
            rule_number=5, check="hard_stop", severity="error",
            message="hard_stop_pct missing from strategy.json or guidance file",
            strategy_value=str(strat_val), guidance_value=guide_val,
        )
    if abs(float(strat_val) - float(guide_val)) > 1e-6:
        return Finding(
            rule_number=5, check="hard_stop", severity="error",
            message=f"Hard-stop mismatch: strategy.json hard_stop_pct={strat_val}%, "
                    f"guidance rule #5={guide_val}%. These MUST match.",
            strategy_value=f"{strat_val}%", guidance_value=f"{guide_val}%",
        )
    return Finding(
        rule_number=5, check="hard_stop", severity="ok",
        message="Hard stop: strategy.json and guidance agree",
        strategy_value=f"{strat_val}%", guidance_value=f"{guide_val}%",
    )
```

- [ ] **Step 1.7: Add a guidance-lint regression test**

Create `tests/test_guidance_lint.py`:

```python
from scorched.services.guidance_lint import _check_stop_loss


def test_stop_loss_passes_when_aligned():
    strategy = {"intraday_monitor": {"hard_stop_pct": 8.0}}
    guidance = "5. **Stop loss at -8% from entry**: Any position down 8%..."
    finding = _check_stop_loss(strategy, guidance)
    assert finding.severity == "ok"


def test_stop_loss_errors_on_mismatch():
    strategy = {"intraday_monitor": {"hard_stop_pct": 5.0}}
    guidance = "5. **Stop loss at -8% from entry**: Any position down 8%..."
    finding = _check_stop_loss(strategy, guidance)
    assert finding.severity == "error"
    assert "5.0" in finding.message and "8" in finding.message


def test_stop_loss_errors_when_strategy_missing():
    strategy = {}
    guidance = "5. **Stop loss at -8% from entry**"
    finding = _check_stop_loss(strategy, guidance)
    assert finding.severity == "error"
```

- [ ] **Step 1.8: Run lint test + run the lint script against the live repo**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_guidance_lint.py -v`
Expected: 3 passed.

Then run the live linter:
Run: `cd /home/ubuntu/tradebot && python3 -m scorched.services.guidance_lint`
Expected: rule 5 hard_stop check shows `OK` (8% in both files). If `ERR`, fix `analyst_guidance.md` rule #5 to read exactly `-8%`.

- [ ] **Step 1.9: Commit**

```bash
git add strategy.json src/scorched/api/intraday.py src/scorched/services/guidance_lint.py tests/test_intraday_endpoint.py tests/test_guidance_lint.py
git commit -m "fix(stop-loss): canonical hard_stop_pct=8.0; intraday reads correct field

Closes audit H4 — code was reading position_drop_from_entry_pct (5%) as
the hard stop while docs declared -8%. Separates the 5% Claude trigger
from the 8% auto-exit. Guidance lint now hard-errors on mismatch."
```

---

## Task 2 — Cash floor uses total portfolio value, cumulative across buys

**Why:** Audit H1. `min_cash = cash_balance * pct` is mathematically wrong (the floor shrinks as cash shrinks). Loop also doesn't decrement cash after accepted buys.

**Files:**
- Create: `src/scorched/risk_gates.py` (NEW — pure functions for re-checks)
- Modify: `src/scorched/services/recommender.py:806-900` (use new gate functions, track running cash)
- Test: `tests/test_risk_gates.py` (NEW)

- [ ] **Step 2.1: Write the failing test**

Create `tests/test_risk_gates.py`:

```python
from decimal import Decimal

from scorched.risk_gates import check_cash_floor, CashFloorResult


class TestCashFloor:
    def test_passes_when_buy_leaves_floor_intact(self):
        # $100k total, $50k cash, 10% floor = $10k min. Buy $30k -> $20k cash. PASS.
        result = check_cash_floor(
            current_cash=Decimal("50000"),
            total_portfolio_value=Decimal("100000"),
            buy_notional=Decimal("30000"),
            reserve_pct=Decimal("0.10"),
        )
        assert result.passed is True
        assert result.projected_cash == Decimal("20000")
        assert result.floor == Decimal("10000")

    def test_rejects_when_buy_breaches_floor(self):
        # $100k total, $50k cash, 10% floor = $10k min. Buy $45k -> $5k cash. FAIL.
        result = check_cash_floor(
            current_cash=Decimal("50000"),
            total_portfolio_value=Decimal("100000"),
            buy_notional=Decimal("45000"),
            reserve_pct=Decimal("0.10"),
        )
        assert result.passed is False
        assert result.projected_cash == Decimal("5000")
        assert result.floor == Decimal("10000")

    def test_floor_uses_total_value_not_cash(self):
        # Regression for audit H1: floor must be based on total, not current cash.
        # If formula were cash * 0.10, floor would be $1000 and buy would pass.
        # Correct formula: total * 0.10 = $10000, buy fails.
        result = check_cash_floor(
            current_cash=Decimal("10000"),
            total_portfolio_value=Decimal("100000"),
            buy_notional=Decimal("5000"),
            reserve_pct=Decimal("0.10"),
        )
        assert result.passed is False  # 10k - 5k = 5k < 10k floor
        assert result.floor == Decimal("10000")  # not Decimal("1000")

    def test_passes_at_exact_floor(self):
        # $100k total, $50k cash, 10% floor = $10k. Buy $40k -> exactly $10k cash.
        result = check_cash_floor(
            current_cash=Decimal("50000"),
            total_portfolio_value=Decimal("100000"),
            buy_notional=Decimal("40000"),
            reserve_pct=Decimal("0.10"),
        )
        assert result.passed is True
        assert result.projected_cash == Decimal("10000")

    def test_zero_total_value_fails_closed(self):
        result = check_cash_floor(
            current_cash=Decimal("0"),
            total_portfolio_value=Decimal("0"),
            buy_notional=Decimal("100"),
            reserve_pct=Decimal("0.10"),
        )
        assert result.passed is False
```

- [ ] **Step 2.2: Verify test fails**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_risk_gates.py::TestCashFloor -v`
Expected: 5 fails — `risk_gates` module does not exist.

- [ ] **Step 2.3: Implement `risk_gates.check_cash_floor`**

Create `src/scorched/risk_gates.py`:

```python
"""Deterministic risk gate functions, callable from recommender and trade-confirm.

Each function returns a small result dataclass with `passed: bool` plus enough
context to log a precise rejection reason. Pure functions — no DB, no I/O —
so they are cheap to re-run at confirm time.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class CashFloorResult:
    passed: bool
    projected_cash: Decimal
    floor: Decimal
    reason: str = ""


def check_cash_floor(
    current_cash: Decimal,
    total_portfolio_value: Decimal,
    buy_notional: Decimal,
    reserve_pct: Decimal,
) -> CashFloorResult:
    """Return PASS only if `current_cash - buy_notional >= total * reserve_pct`.

    `reserve_pct` is a fraction (0.10 = 10%). `total_portfolio_value` is the
    correct base — using `current_cash` collapses the floor as cash shrinks.
    """
    if total_portfolio_value <= 0:
        return CashFloorResult(
            passed=False,
            projected_cash=Decimal("0"),
            floor=Decimal("0"),
            reason="total_portfolio_value is zero — cannot compute floor",
        )
    floor = (Decimal(str(total_portfolio_value)) * Decimal(str(reserve_pct))).quantize(Decimal("0.01"))
    projected = (Decimal(str(current_cash)) - Decimal(str(buy_notional))).quantize(Decimal("0.01"))
    if projected < floor:
        return CashFloorResult(
            passed=False,
            projected_cash=projected,
            floor=floor,
            reason=f"projected cash ${projected:,.2f} < floor ${floor:,.2f}",
        )
    return CashFloorResult(passed=True, projected_cash=projected, floor=floor)
```

- [ ] **Step 2.4: Run test to verify pass**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_risk_gates.py::TestCashFloor -v`
Expected: 5 passed.

- [ ] **Step 2.5: Wire `check_cash_floor` into `recommender.py` with running cash**

In `src/scorched/services/recommender.py`, around line 806 (just before `recommendation_rows = []`), initialize the running cash:

```python
running_cash = Decimal(str(portfolio.cash_balance))
total_value_for_floor = Decimal(str(portfolio_dict["total_value"]))
reserve_pct = Decimal(str(settings.min_cash_reserve_pct))  # already a fraction (0.10)
```

Replace the cash-floor block at lines 829–838 with:

```python
        if action == "buy":
            estimated_cost = suggested_price * quantity

            from ..risk_gates import check_cash_floor
            cash_check = check_cash_floor(
                current_cash=running_cash,
                total_portfolio_value=total_value_for_floor,
                buy_notional=estimated_cost,
                reserve_pct=reserve_pct,
            )
            if not cash_check.passed:
                logger.warning(
                    "Skipping %s buy — cash floor: %s",
                    symbol, cash_check.reason,
                )
                await send_telegram(
                    f"TRADEBOT // Cash reserve gate: {symbol} BUY skipped — {cash_check.reason}"
                )
                continue
```

After accepting a buy (i.e. after the `held_positions_for_sector.append(...)` block around line 893–899), add:

```python
            running_cash = cash_check.projected_cash
```

- [ ] **Step 2.6: Add an integration test for cumulative behavior**

Append to `tests/test_risk_gates.py`:

```python
def test_cumulative_two_buys_breach_floor():
    """Two buys that each fit individually but together breach the floor."""
    cash = Decimal("50000")
    total = Decimal("100000")
    pct = Decimal("0.10")  # floor = $10k
    # First buy $30k -> $20k cash. Passes.
    r1 = check_cash_floor(cash, total, Decimal("30000"), pct)
    assert r1.passed
    # Second buy $15k against running cash $20k -> $5k. Below floor.
    r2 = check_cash_floor(r1.projected_cash, total, Decimal("15000"), pct)
    assert not r2.passed
```

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_risk_gates.py -v`
Expected: 6 passed.

- [ ] **Step 2.7: Run the existing recommender tests to confirm no regression**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_recommender_injection.py tests/test_drawdown_gate.py tests/test_sector_gate.py -v`
Expected: all green. If any fail because they relied on the old buggy formula, update them to the new total-value formula.

- [ ] **Step 2.8: Commit**

```bash
git add src/scorched/risk_gates.py src/scorched/services/recommender.py tests/test_risk_gates.py
git commit -m "fix(gates): cash floor uses total portfolio value and is cumulative

Closes audit H1. Old formula min_cash = cash * pct shrinks the floor as
cash drops, the opposite of intent. New formula uses total portfolio
value, and the recommender loop tracks running cash so multiple buys
in one session cannot collectively breach the 10% floor."
```

---

## Task 3 — Max holdings cumulative across same-session buys

**Why:** Audit H2. `current_count = len(current_positions)` never increments inside the loop, so multiple new-symbol buys can cross the cap.

**Files:**
- Modify: `src/scorched/risk_gates.py` (add `check_holdings_cap`)
- Modify: `src/scorched/services/recommender.py:855-867` (use new function, track running set)
- Test: `tests/test_risk_gates.py`

- [ ] **Step 3.1: Failing test**

Append to `tests/test_risk_gates.py`:

```python
from scorched.risk_gates import check_holdings_cap


class TestHoldingsCap:
    def test_passes_when_under_cap_and_new_symbol(self):
        result = check_holdings_cap(
            held_symbols={"AAPL", "MSFT"},
            accepted_new_symbols=set(),
            proposed_symbol="NVDA",
            max_holdings=10,
        )
        assert result.passed is True

    def test_rejects_at_cap_with_new_symbol(self):
        held = {f"S{i}" for i in range(10)}
        result = check_holdings_cap(
            held_symbols=held,
            accepted_new_symbols=set(),
            proposed_symbol="NEW",
            max_holdings=10,
        )
        assert result.passed is False

    def test_add_to_existing_holding_passes_at_cap(self):
        held = {f"S{i}" for i in range(10)}
        result = check_holdings_cap(
            held_symbols=held,
            accepted_new_symbols=set(),
            proposed_symbol="S0",  # already held
            max_holdings=10,
        )
        assert result.passed is True

    def test_cumulative_new_buys_breach_cap(self):
        held = {f"S{i}" for i in range(8)}
        # Two prior new buys accepted -> 10 effective holdings.
        accepted = {"NEW1", "NEW2"}
        result = check_holdings_cap(
            held_symbols=held,
            accepted_new_symbols=accepted,
            proposed_symbol="NEW3",
            max_holdings=10,
        )
        assert result.passed is False
```

- [ ] **Step 3.2: Verify it fails**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_risk_gates.py::TestHoldingsCap -v`
Expected: 4 fails — function does not exist.

- [ ] **Step 3.3: Implement `check_holdings_cap`**

Append to `src/scorched/risk_gates.py`:

```python
@dataclass
class HoldingsCapResult:
    passed: bool
    projected_count: int
    cap: int
    reason: str = ""


def check_holdings_cap(
    held_symbols: set[str],
    accepted_new_symbols: set[str],
    proposed_symbol: str,
    max_holdings: int,
) -> HoldingsCapResult:
    """Return PASS unless adding `proposed_symbol` would exceed `max_holdings`.

    Adding to an *existing* holding does not increase the count. Only buys of
    new symbols not already in `held_symbols` or `accepted_new_symbols` count.
    """
    proposed = proposed_symbol.upper()
    held_upper = {s.upper() for s in held_symbols}
    accepted_upper = {s.upper() for s in accepted_new_symbols}

    if proposed in held_upper or proposed in accepted_upper:
        return HoldingsCapResult(
            passed=True,
            projected_count=len(held_upper | accepted_upper),
            cap=max_holdings,
            reason="add to existing holding — does not increase count",
        )

    projected = len(held_upper | accepted_upper) + 1
    if projected > max_holdings:
        return HoldingsCapResult(
            passed=False,
            projected_count=projected,
            cap=max_holdings,
            reason=f"would create holding #{projected} > cap {max_holdings}",
        )
    return HoldingsCapResult(passed=True, projected_count=projected, cap=max_holdings)
```

- [ ] **Step 3.4: Run test to verify pass**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_risk_gates.py::TestHoldingsCap -v`
Expected: 4 passed.

- [ ] **Step 3.5: Wire into recommender**

In `src/scorched/services/recommender.py`, near the running-cash init from Task 2 (around line 806), add:

```python
held_symbol_set = {p["symbol"].upper() for p in current_positions}
accepted_new_symbols: set[str] = set()
```

Replace the holdings-gate block at lines 855–867 with:

```python
            from ..risk_gates import check_holdings_cap
            holdings_check = check_holdings_cap(
                held_symbols=held_symbol_set,
                accepted_new_symbols=accepted_new_symbols,
                proposed_symbol=symbol,
                max_holdings=strategy_conc.get("max_holdings", 10),
            )
            if not holdings_check.passed:
                logger.warning(
                    "Skipping %s buy — holdings cap: %s", symbol, holdings_check.reason,
                )
                await send_telegram(
                    f"TRADEBOT // Holdings gate: {symbol} BUY skipped — {holdings_check.reason}"
                )
                continue
```

After the buy is accepted (same place where Task 2 updates `running_cash`), add:

```python
            if symbol.upper() not in held_symbol_set:
                accepted_new_symbols.add(symbol.upper())
```

- [ ] **Step 3.6: Run full risk-gates suite + recommender tests**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_risk_gates.py tests/test_recommender_injection.py -v`
Expected: all passed.

- [ ] **Step 3.7: Commit**

```bash
git add src/scorched/risk_gates.py src/scorched/services/recommender.py tests/test_risk_gates.py
git commit -m "fix(gates): max holdings cumulative across same-session buys

Closes audit H2. Adds running set of accepted new symbols so successive
buys see the correct projected count. Add-to-existing buys do not count
toward the holdings cap (decision D-implicit in plan)."
```

---

## Task 4 — Max position cap on post-trade total exposure

**Why:** Audit H3. Existing position market value plus the proposed buy can exceed the 33% cap while passing the current `estimated_cost > max_pos_dollars` check.

**Files:**
- Modify: `src/scorched/risk_gates.py` (add `check_position_cap`)
- Modify: `src/scorched/services/recommender.py:840-853` (use new function)
- Test: `tests/test_risk_gates.py`

- [ ] **Step 4.1: Failing test**

Append to `tests/test_risk_gates.py`:

```python
from scorched.risk_gates import check_position_cap


class TestPositionCap:
    def test_pure_new_buy_under_cap_passes(self):
        # $100k total, 33% cap = $33k. Buy $30k. PASS.
        result = check_position_cap(
            existing_market_value=Decimal("0"),
            buy_notional=Decimal("30000"),
            total_portfolio_value=Decimal("100000"),
            max_position_pct=Decimal("33"),
        )
        assert result.passed is True

    def test_pure_new_buy_over_cap_rejects(self):
        result = check_position_cap(
            existing_market_value=Decimal("0"),
            buy_notional=Decimal("35000"),
            total_portfolio_value=Decimal("100000"),
            max_position_pct=Decimal("33"),
        )
        assert result.passed is False

    def test_add_on_buy_post_trade_breaches_cap(self):
        # Existing $25k, buy $10k -> $35k post-trade vs $33k cap. FAIL.
        result = check_position_cap(
            existing_market_value=Decimal("25000"),
            buy_notional=Decimal("10000"),
            total_portfolio_value=Decimal("100000"),
            max_position_pct=Decimal("33"),
        )
        assert result.passed is False
        assert result.projected_pct > 33

    def test_add_on_buy_within_cap_passes(self):
        # Existing $20k, buy $10k -> $30k. PASS.
        result = check_position_cap(
            existing_market_value=Decimal("20000"),
            buy_notional=Decimal("10000"),
            total_portfolio_value=Decimal("100000"),
            max_position_pct=Decimal("33"),
        )
        assert result.passed is True
```

- [ ] **Step 4.2: Verify failure**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_risk_gates.py::TestPositionCap -v`
Expected: 4 fails — function not defined.

- [ ] **Step 4.3: Implement**

Append to `src/scorched/risk_gates.py`:

```python
@dataclass
class PositionCapResult:
    passed: bool
    projected_pct: float
    cap_pct: float
    reason: str = ""


def check_position_cap(
    existing_market_value: Decimal,
    buy_notional: Decimal,
    total_portfolio_value: Decimal,
    max_position_pct: Decimal,
) -> PositionCapResult:
    """Reject if `(existing + buy) / total * 100 > max_position_pct`."""
    if total_portfolio_value <= 0:
        return PositionCapResult(
            passed=False,
            projected_pct=0.0,
            cap_pct=float(max_position_pct),
            reason="total_portfolio_value is zero",
        )
    post_trade_value = Decimal(str(existing_market_value)) + Decimal(str(buy_notional))
    pct = float(post_trade_value) / float(total_portfolio_value) * 100
    cap = float(max_position_pct)
    if pct > cap:
        return PositionCapResult(
            passed=False,
            projected_pct=pct,
            cap_pct=cap,
            reason=f"post-trade exposure {pct:.1f}% > cap {cap:.1f}%",
        )
    return PositionCapResult(passed=True, projected_pct=pct, cap_pct=cap)
```

- [ ] **Step 4.4: Verify pass**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_risk_gates.py::TestPositionCap -v`
Expected: 4 passed.

- [ ] **Step 4.5: Wire into recommender**

In `src/scorched/services/recommender.py`, near the running-cash init (around line 806), build a per-symbol exposure map:

```python
existing_position_value: dict[str, Decimal] = {}
for p in current_positions:
    existing_position_value[p["symbol"].upper()] = Decimal(str(p.get("market_value", 0) or 0))
```

Replace the position-cap block at lines 840–853 with:

```python
            from ..risk_gates import check_position_cap
            existing_value = existing_position_value.get(symbol.upper(), Decimal("0"))
            position_check = check_position_cap(
                existing_market_value=existing_value,
                buy_notional=estimated_cost,
                total_portfolio_value=total_value_for_floor,
                max_position_pct=Decimal(str(strategy_conc.get("max_position_pct", 33))),
            )
            if not position_check.passed:
                logger.warning(
                    "Skipping %s buy — position cap: %s", symbol, position_check.reason,
                )
                await send_telegram(
                    f"TRADEBOT // Position size gate: {symbol} BUY skipped — {position_check.reason}"
                )
                continue
```

After accepting a buy, update the running map:

```python
            existing_position_value[symbol.upper()] = existing_value + estimated_cost
```

- [ ] **Step 4.6: Run full suite**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_risk_gates.py tests/test_recommender_injection.py -v`
Expected: green.

- [ ] **Step 4.7: Commit**

```bash
git add src/scorched/risk_gates.py src/scorched/services/recommender.py tests/test_risk_gates.py
git commit -m "fix(gates): position cap applies to post-trade total exposure

Closes audit H3. Add-on buys to existing positions now include current
market value in the cap check. Tracks per-symbol exposure across the
recommendation loop so two adds in one session cannot stack past 33%."
```

---

## Task 5 — Sector gate fails closed on unknown sector + Finnhub fallback

**Why:** Decision D4. Audit M10. Current `check_sector_exposure` returns True with a warning when sector is None — a 40% cap that punts on missing data is no cap.

**Files:**
- Modify: `src/scorched/services/recommender.py:268-294` (`check_sector_exposure` rejects on unknown)
- Modify: `src/scorched/services/recommender.py:254-265` (`_get_sector_for_symbol` falls back to Finnhub)
- Modify: `src/scorched/services/finnhub_data.py` (add `fetch_sector_for_symbol`)
- Test: `tests/test_sector_gate.py` (extend) and `tests/test_finnhub_data.py` (extend)

- [ ] **Step 5.1: Failing test for fail-closed sector gate**

Append to `tests/test_sector_gate.py`:

```python
from decimal import Decimal

from scorched.services.recommender import check_sector_exposure


class TestSectorGateFailClosed:
    def test_unknown_sector_now_rejects(self):
        """Regression for audit M10: was returning True with warning."""
        result = check_sector_exposure(
            proposed_symbol="UNKNOWN",
            proposed_sector=None,
            proposed_dollars=Decimal("10000"),
            held_positions=[],
            total_value=Decimal("100000"),
            max_sector_pct=40.0,
        )
        assert result is False  # fail closed

    def test_known_sector_within_cap_still_passes(self):
        result = check_sector_exposure(
            proposed_symbol="AAPL",
            proposed_sector="Technology",
            proposed_dollars=Decimal("10000"),
            held_positions=[],
            total_value=Decimal("100000"),
            max_sector_pct=40.0,
        )
        assert result is True
```

- [ ] **Step 5.2: Verify failure**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_sector_gate.py::TestSectorGateFailClosed -v`
Expected: `test_unknown_sector_now_rejects` FAILS — current behavior returns True.

- [ ] **Step 5.3: Update `check_sector_exposure` to fail closed**

In `src/scorched/services/recommender.py`, replace lines 286–294 (the `if proposed_sector is None:` block) with:

```python
    if proposed_sector is None:
        logger.warning(
            "Sector gate REJECT %s: unknown sector — failing closed (audit M10)",
            proposed_symbol,
        )
        return False
```

- [ ] **Step 5.4: Verify gate test passes**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_sector_gate.py -v`
Expected: green.

- [ ] **Step 5.5: Failing test for Finnhub fallback**

Append to `tests/test_finnhub_data.py` (or create `TestSectorFallback` class if missing):

```python
from unittest.mock import patch, MagicMock

from scorched.services.finnhub_data import fetch_sector_for_symbol


class TestSectorFallback:
    def test_returns_finnhub_industry_when_available(self):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "ticker": "PLTR",
            "finnhubIndustry": "Technology",
            "name": "Palantir",
        }
        with patch("scorched.services.finnhub_data._http_get", return_value=fake_response):
            sector = fetch_sector_for_symbol("PLTR")
        assert sector == "Technology"

    def test_returns_none_when_finnhub_returns_no_industry(self):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"ticker": "WEIRD"}
        with patch("scorched.services.finnhub_data._http_get", return_value=fake_response):
            sector = fetch_sector_for_symbol("WEIRD")
        assert sector is None

    def test_returns_none_when_no_api_key(self):
        with patch("scorched.services.finnhub_data.settings") as mock_s:
            mock_s.finnhub_api_key = ""
            sector = fetch_sector_for_symbol("AAPL")
        assert sector is None
```

- [ ] **Step 5.6: Implement Finnhub fallback**

`src/scorched/services/finnhub_data.py` already imports `retry_call` from `..http_retry`. Use the same pattern as `fetch_analyst_consensus_sync`. Add this function (and `import requests` near the top of the file if not already present — check with `grep -n "^import\|^from" /home/ubuntu/tradebot/src/scorched/services/finnhub_data.py` before editing):

```python
def fetch_sector_for_symbol(symbol: str) -> str | None:
    """Fetch GICS sector from Finnhub stock/profile2 endpoint. Returns None on failure.

    Used as fallback when the static `_SECTOR_ETF_MAP` has no entry for `symbol`.
    Finnhub's `finnhubIndustry` field is GICS-aligned for the major sectors.
    """
    from ..config import settings  # local import keeps this file's existing pattern
    import requests

    if not settings.finnhub_api_key:
        return None
    url = "https://finnhub.io/api/v1/stock/profile2"
    params = {"symbol": symbol.upper(), "token": settings.finnhub_api_key}
    try:
        response = retry_call(lambda: requests.get(url, params=params, timeout=10))
        if response is None or response.status_code != 200:
            return None
        data = response.json()
        industry = data.get("finnhubIndustry")
        return industry if industry else None
    except Exception as exc:
        logger.warning("Finnhub sector lookup failed for %s: %s", symbol, exc)
        return None
```

- [ ] **Step 5.7: Wire fallback into `_get_sector_for_symbol`**

In `src/scorched/services/recommender.py`, replace lines 254–265 with:

```python
def _get_sector_for_symbol(symbol: str) -> str | None:
    """Return GICS sector for the symbol; uses static ETF map first, Finnhub second."""
    from .research import _SECTOR_ETF_MAP
    from .finnhub_data import fetch_sector_for_symbol

    etf = _SECTOR_ETF_MAP.get(symbol)
    if etf is not None:
        sector = _ETF_TO_SECTOR.get(etf)
        if sector:
            return sector

    # Fallback: ask Finnhub.
    return fetch_sector_for_symbol(symbol)
```

- [ ] **Step 5.8: Run all sector + finnhub tests**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_sector_gate.py tests/test_finnhub_data.py -v`
Expected: green. If `test_finnhub_data.py` had different mock patterns, adjust the new tests to match.

- [ ] **Step 5.9: Commit**

```bash
git add src/scorched/services/recommender.py src/scorched/services/finnhub_data.py tests/test_sector_gate.py tests/test_finnhub_data.py
git commit -m "fix(sector): fail closed on unknown sector + Finnhub fallback

Closes audit M10 / decision D4. Sector gate now rejects buys with no
sector metadata. Finnhub stock/profile2 supplies GICS industry as a
fallback for symbols missing from the static sector ETF map."
```

---

## Task 6 — `/trades/confirm` is server-decides; re-runs all gates

**Why:** Audit C1 + decision D3. The single largest live-mode risk. Stored recommendation becomes the source of truth for quantity and price; client-supplied `shares`/`execution_price` become advisory.

**Depends on:** Tasks 2–5 (re-runs those gate functions).

**Files:**
- Modify: `src/scorched/schemas.py:43-46` (make `shares`/`execution_price` Optional)
- Modify: `src/scorched/api/trades.py:21-91` (server-decides + re-run gates)
- Modify: `cron/tradebot_phase2.py` (drop client price/qty from confirm payload)
- Modify: `src/scorched/risk_gates.py` (add `run_all_buy_gates` aggregate)
- Test: `tests/test_trades_confirm.py` (NEW)

- [ ] **Step 6.1: Write failing tests**

Create `tests/test_trades_confirm.py`:

```python
"""Tests for the hardened /trades/confirm endpoint (audit C1)."""
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from scorched.main import app
from scorched.models import TradeRecommendation


@pytest.mark.asyncio
async def test_confirm_uses_stored_rec_quantity_not_client_qty(db_session):
    """Audit C1: client-supplied shares must not override stored rec quantity."""
    rec = TradeRecommendation(
        symbol="AAPL", action="buy", quantity=Decimal("10"),
        suggested_price=Decimal("150.00"), confidence="high",
        reasoning="test", key_risks="", status="pending",
        session_id=None, recommended_at=date.today(),
    )
    db_session.add(rec)
    await db_session.commit()

    fake_broker = AsyncMock()
    fake_broker.submit_buy.return_value = {"status": "submitted", "filled_qty": Decimal("10"), "filled_avg_price": Decimal("150")}

    with patch("scorched.api.trades.get_broker", return_value=fake_broker), \
         patch("scorched.api.trades.fetch_live_price", return_value=Decimal("150.50")), \
         patch("scorched.api.trades.run_all_buy_gates") as mock_gates:
        mock_gates.return_value.passed = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/trades/confirm",
                json={"recommendation_id": rec.id, "shares": "9999", "execution_price": "1.00"},
            )

    assert r.status_code == 200
    fake_broker.submit_buy.assert_called_once()
    kwargs = fake_broker.submit_buy.call_args.kwargs
    assert kwargs["qty"] == Decimal("10")  # from stored rec, not 9999


@pytest.mark.asyncio
async def test_confirm_rejects_when_gates_fail(db_session):
    """If cash floor / position cap / etc fail at confirm time, broker is NOT called."""
    rec = TradeRecommendation(
        symbol="AAPL", action="buy", quantity=Decimal("10"),
        suggested_price=Decimal("150.00"), confidence="high",
        reasoning="test", key_risks="", status="pending",
        session_id=None, recommended_at=date.today(),
    )
    db_session.add(rec)
    await db_session.commit()

    fake_broker = AsyncMock()

    with patch("scorched.api.trades.get_broker", return_value=fake_broker), \
         patch("scorched.api.trades.fetch_live_price", return_value=Decimal("150.50")), \
         patch("scorched.api.trades.run_all_buy_gates") as mock_gates:
        mock_gates.return_value.passed = False
        mock_gates.return_value.reason = "cash floor would breach"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/trades/confirm",
                json={"recommendation_id": rec.id},
            )

    assert r.status_code == 422
    assert "cash floor" in r.text.lower()
    fake_broker.submit_buy.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_rejects_when_live_price_drifts_beyond_tolerance(db_session):
    """Stored price was $150; live $200 -> 33% drift > 5% tolerance -> reject."""
    rec = TradeRecommendation(
        symbol="AAPL", action="buy", quantity=Decimal("10"),
        suggested_price=Decimal("150.00"), confidence="high",
        reasoning="test", key_risks="", status="pending",
        session_id=None, recommended_at=date.today(),
    )
    db_session.add(rec)
    await db_session.commit()

    fake_broker = AsyncMock()

    with patch("scorched.api.trades.get_broker", return_value=fake_broker), \
         patch("scorched.api.trades.fetch_live_price", return_value=Decimal("200.00")):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/trades/confirm",
                json={"recommendation_id": rec.id},
            )

    assert r.status_code == 422
    assert "drift" in r.text.lower() or "tolerance" in r.text.lower()
    fake_broker.submit_buy.assert_not_called()
```

- [ ] **Step 6.2: Verify failure**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_trades_confirm.py -v`
Expected: 3 fails.

- [ ] **Step 6.3: Add aggregate `run_all_buy_gates` to `risk_gates.py`**

Append to `src/scorched/risk_gates.py`:

```python
@dataclass
class BuyGatesResult:
    passed: bool
    reason: str = ""
    details: dict | None = None


def run_all_buy_gates(
    *,
    symbol: str,
    sector: str | None,
    buy_notional: Decimal,
    current_cash: Decimal,
    total_portfolio_value: Decimal,
    held_symbols: set[str],
    held_positions_with_sector: list[dict],
    existing_position_value: Decimal,
    reserve_pct: Decimal,
    max_position_pct: Decimal,
    max_sector_pct: float,
    max_holdings: int,
) -> BuyGatesResult:
    """Run cash floor + holdings + position cap + sector gates in one shot."""
    cash = check_cash_floor(current_cash, total_portfolio_value, buy_notional, reserve_pct)
    if not cash.passed:
        return BuyGatesResult(passed=False, reason=f"cash_floor: {cash.reason}", details={"cash": cash.__dict__})

    holdings = check_holdings_cap(held_symbols, set(), symbol, max_holdings)
    if not holdings.passed:
        return BuyGatesResult(passed=False, reason=f"holdings: {holdings.reason}", details={"holdings": holdings.__dict__})

    pos = check_position_cap(existing_position_value, buy_notional, total_portfolio_value, max_position_pct)
    if not pos.passed:
        return BuyGatesResult(passed=False, reason=f"position_cap: {pos.reason}", details={"position": pos.__dict__})

    # Sector check (lives in recommender; import locally to avoid cycle)
    from .services.recommender import check_sector_exposure
    sector_ok = check_sector_exposure(
        proposed_symbol=symbol,
        proposed_sector=sector,
        proposed_dollars=buy_notional,
        held_positions=held_positions_with_sector,
        total_value=total_portfolio_value,
        max_sector_pct=max_sector_pct,
    )
    if not sector_ok:
        return BuyGatesResult(passed=False, reason="sector_cap: would breach", details=None)

    return BuyGatesResult(passed=True)
```

- [ ] **Step 6.4: Make schema fields advisory**

In `src/scorched/schemas.py:43-46`, replace `ConfirmTradeRequest` with:

```python
class ConfirmTradeRequest(BaseModel):
    recommendation_id: int
    # Advisory only — server uses stored recommendation values. These are
    # accepted for backwards compatibility with old cron payloads but ignored
    # except for a sanity-warning on large mismatch (logged, not rejected).
    execution_price: Decimal | None = None
    shares: Decimal | None = None
```

- [ ] **Step 6.5: Rewrite `/trades/confirm`**

Replace the body of `confirm_trade` in `src/scorched/api/trades.py:21-124` with the following. Add helper imports at the top of the file:

```python
from decimal import Decimal
from sqlalchemy import select
from ..config import settings
from ..models import Portfolio, Position, TradeRecommendation
from ..risk_gates import run_all_buy_gates
from ..services.alpaca_data import fetch_snapshots_sync
from ..services.recommender import _get_sector_for_symbol


PRICE_DRIFT_TOLERANCE_PCT = Decimal("5.0")


def fetch_live_price(symbol: str) -> Decimal | None:
    """Fetch current price via Alpaca snapshot. None on failure."""
    try:
        snaps = fetch_snapshots_sync([symbol])
        if symbol in snaps:
            return Decimal(str(snaps[symbol]["current_price"]))
    except Exception:
        pass
    return None
```

Replace the endpoint body:

```python
@router.post("/confirm", response_model=ConfirmTradeResponse, dependencies=[Depends(require_owner_pin)])
async def confirm_trade(body: ConfirmTradeRequest, db: AsyncSession = Depends(get_db)):
    rec = (
        await db.execute(
            select(TradeRecommendation).where(TradeRecommendation.id == body.recommendation_id)
        )
    ).scalars().first()

    if rec is None:
        raise HTTPException(status_code=404, detail=f"Recommendation {body.recommendation_id} not found")
    if rec.status not in ("pending", "submitted"):
        raise HTTPException(status_code=409, detail=f"Recommendation is already {rec.status}")

    # Server decides everything — client shares/price are advisory only.
    qty = Decimal(str(rec.quantity))
    stored_price = Decimal(str(rec.suggested_price))

    live_price = fetch_live_price(rec.symbol)
    if live_price is None:
        raise HTTPException(status_code=503, detail=f"Cannot fetch live price for {rec.symbol}")

    drift_pct = abs(live_price - stored_price) / stored_price * 100
    if drift_pct > PRICE_DRIFT_TOLERANCE_PCT:
        raise HTTPException(
            status_code=422,
            detail=f"Price drift {drift_pct:.1f}% exceeds {PRICE_DRIFT_TOLERANCE_PCT}% tolerance — stored ${stored_price}, live ${live_price}",
        )

    # Compute final limit price with strategy buffer.
    import json
    from pathlib import Path
    strategy_path = Path(__file__).resolve().parent.parent.parent.parent / "strategy.json"
    with open(strategy_path) as f:
        strategy = json.load(f)
    exec_cfg = strategy.get("execution", {})
    if rec.action == "buy":
        buf = Decimal(str(exec_cfg.get("buy_limit_buffer_pct", 0.3))) / Decimal("100")
        limit_price = (live_price * (Decimal("1") + buf)).quantize(Decimal("0.01"))
    else:
        buf = Decimal(str(exec_cfg.get("sell_limit_buffer_pct", 0.3))) / Decimal("100")
        limit_price = (live_price * (Decimal("1") - buf)).quantize(Decimal("0.01"))

    # Re-run all buy-side gates immediately before broker submission.
    if rec.action == "buy":
        portfolio = (await db.execute(select(Portfolio))).scalars().first()
        held = (await db.execute(select(Position))).scalars().all()
        held_symbols = {p.symbol.upper() for p in held}

        # Total value with live prices.
        from ..services.portfolio import _compute_portfolio_total_value
        total_value = await _compute_portfolio_total_value(
            Decimal(str(portfolio.cash_balance)),
            [{"symbol": p.symbol, "shares": p.shares, "avg_cost_basis": p.avg_cost_basis} for p in held],
            {},
        )

        existing_pos = next((p for p in held if p.symbol.upper() == rec.symbol.upper()), None)
        existing_value = (Decimal(str(existing_pos.shares)) * live_price) if existing_pos else Decimal("0")

        held_with_sector = [
            {"symbol": p.symbol, "sector": _get_sector_for_symbol(p.symbol),
             "market_value": Decimal(str(p.shares)) * live_price}
            for p in held
        ]
        conc = strategy.get("concentration", {})

        gate_result = run_all_buy_gates(
            symbol=rec.symbol,
            sector=_get_sector_for_symbol(rec.symbol),
            buy_notional=qty * live_price,
            current_cash=Decimal(str(portfolio.cash_balance)),
            total_portfolio_value=total_value,
            held_symbols=held_symbols,
            held_positions_with_sector=held_with_sector,
            existing_position_value=existing_value,
            reserve_pct=Decimal(str(settings.min_cash_reserve_pct)),
            max_position_pct=Decimal(str(conc.get("max_position_pct", 33))),
            max_sector_pct=float(conc.get("max_sector_pct", 40)),
            max_holdings=int(conc.get("max_holdings", 10)),
        )
        if not gate_result.passed:
            raise HTTPException(status_code=422, detail=f"Risk gate rejected at confirm time: {gate_result.reason}")

    broker = get_broker(db)
    try:
        if rec.action == "buy":
            result = await broker.submit_buy(
                symbol=rec.symbol, qty=qty, limit_price=limit_price, recommendation_id=body.recommendation_id,
            )
        else:
            result = await broker.submit_sell(
                symbol=rec.symbol, qty=qty, limit_price=limit_price, recommendation_id=body.recommendation_id,
            )
    except Exception as exc:
        logger.error("Broker order failed: symbol=%s rec_id=%s error=%s", rec.symbol, body.recommendation_id, exc, exc_info=True)
        await send_telegram(
            f"🚨 BROKER ERROR — order may have filled on Alpaca!\nSymbol: {rec.symbol}\nAction: {rec.action}\nShares: {qty}\nRecommendation ID: {body.recommendation_id}\nError: {exc}"
        )
        raise HTTPException(status_code=500, detail=f"Broker call failed: {exc}")

    if result["status"] == "submitted":
        rec.status = "submitted"
        await db.commit()
        return ConfirmTradeResponse(
            trade_id=0, symbol=rec.symbol, action=rec.action,
            shares=result["filled_qty"], execution_price=result["filled_avg_price"],
            total_value=Decimal("0"), new_cash_balance=Decimal("0"),
            position=None, realized_gain=None, tax_category=None,
        )

    if result["status"] != "filled":
        raise HTTPException(status_code=422, detail=f"Order not filled: status={result['status']}")

    pos = (await db.execute(select(Position).where(Position.symbol == rec.symbol))).scalars().first()
    position_detail = None
    if pos:
        position_detail = PositionDetail(
            symbol=pos.symbol, shares=pos.shares,
            avg_cost_basis=pos.avg_cost_basis, first_purchase_date=pos.first_purchase_date,
        )
    return ConfirmTradeResponse(
        trade_id=result.get("trade_id", 0),
        symbol=rec.symbol, action=rec.action,
        shares=result["filled_qty"], execution_price=result["filled_avg_price"],
        total_value=(result["filled_qty"] * result["filled_avg_price"]).quantize(Decimal("0.01")),
        new_cash_balance=result.get("new_cash_balance", Decimal("0")),
        position=position_detail,
        realized_gain=result.get("realized_gain"),
        tax_category=result.get("tax_category"),
    )
```

- [ ] **Step 6.6: Update Phase 2 cron payload**

Inspect `cron/tradebot_phase2.py` for the call to `/api/v1/trades/confirm`. Wherever the JSON body is built with `shares` and `execution_price`, simplify to just `{"recommendation_id": rec_id}` (or leave the fields as informational — the server will ignore them). Confirm by grep:

Run: `grep -n "execution_price\|shares" /home/ubuntu/tradebot/cron/tradebot_phase2.py`

Adjust the payload construction so it only requires `recommendation_id`. The other fields can remain for backwards compatibility — the server ignores them.

- [ ] **Step 6.7: Run all confirm tests + recommender tests**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_trades_confirm.py tests/test_risk_gates.py -v`
Expected: green.

- [ ] **Step 6.8: Run full suite to catch regressions**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest -q`
Expected: same pass count or better than before this task. Acceptable: previously-failing onboarding-auth tests still fail (Task 9 fixes those). Any new failure must be diagnosed before commit.

- [ ] **Step 6.9: Commit**

```bash
git add src/scorched/schemas.py src/scorched/api/trades.py src/scorched/risk_gates.py cron/tradebot_phase2.py tests/test_trades_confirm.py
git commit -m "feat(trades): server-decides confirm + re-run gates at submission

Closes audit C1. /trades/confirm now uses the stored recommendation as
the source of truth for quantity and price. Client shares/execution_price
are accepted for backwards compatibility but ignored. Live price is
fetched fresh, drift beyond 5% rejects the confirm, and all buy-side
risk gates re-run before the broker call. Phase 2 payload simplified."
```

---

## Task 7 — Marketable hard-stop limit price + retry without cooldown

**Why:** Audit H5/H6. Current hard-stop `_execute_sell` uses fresh price as the limit with no buffer; in falling markets the limit sits above bid and never fills. Cooldown also suppresses retries on failure.

**Files:**
- Modify: `src/scorched/api/intraday.py:65-122` (apply emergency sell buffer for hard-stop path)
- Modify: `cron/intraday_monitor.py` (cooldown only on success — find file, inspect lines 352–362)
- Modify: `strategy.json` (add `intraday_monitor.emergency_sell_buffer_pct: 1.0`)
- Test: `tests/test_intraday_endpoint.py`

- [ ] **Step 7.1: Failing test for emergency buffer**

Append to `tests/test_intraday_endpoint.py`:

```python
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from scorched.api.intraday import _compute_emergency_sell_limit


class TestEmergencyLimit:
    def test_applies_buffer_below_current(self):
        # Current $100, buffer 1% -> limit $99.00
        limit = _compute_emergency_sell_limit(current_price=Decimal("100"), buffer_pct=Decimal("1.0"))
        assert limit == Decimal("99.00")

    def test_applies_buffer_below_at_higher_price(self):
        # Current $250.50, buffer 1% -> $247.99 (rounded)
        limit = _compute_emergency_sell_limit(current_price=Decimal("250.50"), buffer_pct=Decimal("1.0"))
        assert limit == Decimal("247.99") or limit == Decimal("248.00")  # rounding tolerance

    def test_zero_buffer_returns_current(self):
        limit = _compute_emergency_sell_limit(current_price=Decimal("100"), buffer_pct=Decimal("0"))
        assert limit == Decimal("100.00")
```

- [ ] **Step 7.2: Verify failure**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_intraday_endpoint.py::TestEmergencyLimit -v`
Expected: ImportError / function not defined.

- [ ] **Step 7.3: Implement emergency-limit helper + use it on hard-stop path**

In `src/scorched/api/intraday.py`, add this helper near `_fresh_price`:

```python
def _compute_emergency_sell_limit(current_price: Decimal, buffer_pct: Decimal) -> Decimal:
    """Return a marketable limit price below current = current * (1 - buffer/100)."""
    buf = Decimal(str(buffer_pct)) / Decimal("100")
    return (Decimal(str(current_price)) * (Decimal("1") - buf)).quantize(Decimal("0.01"))


def _load_emergency_buffer_pct() -> Decimal:
    try:
        with open(STRATEGY_PATH) as f:
            data = json.load(f)
        return Decimal(str(data.get("intraday_monitor", {}).get("emergency_sell_buffer_pct", 1.0)))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return Decimal("1.0")
```

Update `_execute_sell` (lines 65–122) so the hard-stop call site (in `evaluate_triggers`) can request emergency pricing. Add a parameter:

```python
async def _execute_sell(
    trigger: IntradayTriggerItem,
    sell_qty: Decimal,
    db: AsyncSession,
    use_emergency_limit: bool = False,
) -> tuple[dict | None, str | None]:
    broker = get_broker(db)

    loop = asyncio.get_running_loop()
    fresh = await loop.run_in_executor(None, _fresh_price, trigger.symbol)
    base = fresh or Decimal(str(trigger.current_price))
    if use_emergency_limit:
        limit_price = _compute_emergency_sell_limit(base, _load_emergency_buffer_pct())
    else:
        limit_price = base.quantize(Decimal("0.01"))

    # ... rest unchanged
```

In `evaluate_triggers` around line 169 where the hard-stop `_execute_sell` is invoked, pass `use_emergency_limit=True`:

```python
            trade_result, err = await _execute_sell(trigger, trigger.shares, db, use_emergency_limit=True)
```

- [ ] **Step 7.4: Add `emergency_sell_buffer_pct` to strategy.json**

In `strategy.json`, inside `intraday_monitor`, add the field:

```json
    "emergency_sell_buffer_pct": 1.0
```

So that block reads (final form):

```json
  "intraday_monitor": {
    "enabled": true,
    "position_drop_from_entry_pct": 5.0,
    "position_drop_from_open_pct": 3.0,
    "spy_intraday_drop_pct": 2.0,
    "volume_surge_multiplier": 3.0,
    "hard_stop_pct": 8.0,
    "emergency_sell_buffer_pct": 1.0
  },
```

- [ ] **Step 7.5: Verify emergency-limit test passes**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_intraday_endpoint.py::TestEmergencyLimit -v`
Expected: green.

- [ ] **Step 7.6: Cooldown only on successful terminal action**

Inspect `cron/intraday_monitor.py` lines 340–380:

Run: `sed -n '340,380p' /home/ubuntu/tradebot/cron/intraday_monitor.py`

Find the `record_cooldown` (or equivalent) call. Move it so that it only fires when the API response indicates the position was actually exited (action == `exit_full` or `exit_partial`) AND `trade_result` is non-null. Hard-stop-attempt failures must NOT record a cooldown.

Concretely, inside the per-position loop after the API response is parsed:

```python
            decision = response_data["decisions"][i]
            action = decision.get("action")
            trade_result = decision.get("trade_result")
            sold_successfully = action in ("exit_full", "exit_partial") and trade_result is not None
            if sold_successfully:
                record_cooldown(symbol)
            else:
                logger.info("No cooldown for %s — action=%s trade_result=%s", symbol, action, trade_result)
```

Adjust to match the actual variable names in the file. The principle: cooldown only on successful terminal action.

- [ ] **Step 7.7: Run intraday tests + commit**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_intraday.py tests/test_intraday_endpoint.py -v`
Expected: green.

```bash
git add src/scorched/api/intraday.py cron/intraday_monitor.py strategy.json tests/test_intraday_endpoint.py
git commit -m "fix(intraday): marketable hard-stop limit + retry on failure

Closes audit H5/H6. Hard-stop sells now apply a configurable emergency
sell buffer (default 1%) so the limit sits below current rather than at
current — survives fast falling markets. Cooldown is only recorded on
successful terminal action; failed hard-stop sells retry on the next
5-min tick."
```

---

## Task 8 — Circuit breaker uses Alpaca snapshots + gap-up gate wired

**Why:** Audit M2/M3. yfinance daily history is delayed at 9:55 AM; `check_gap_up_gate` exists but is never called.

**Files:**
- Modify: `src/scorched/circuit_breaker.py:117-201` (rewrite `fetch_gate_data` to use Alpaca; call `check_gap_up_gate` from `run_circuit_breaker`)
- Test: `tests/test_circuit_breaker.py` (extend)

- [ ] **Step 8.1: Failing test**

Append to `tests/test_circuit_breaker.py`:

```python
from decimal import Decimal
from unittest.mock import patch, AsyncMock

import pytest

from scorched.circuit_breaker import run_circuit_breaker


@pytest.mark.asyncio
async def test_circuit_breaker_uses_alpaca_snapshots():
    """Audit M2: should use Alpaca snapshots, not yfinance."""
    recs = [{"symbol": "AAPL", "action": "buy", "suggested_price": 150.0}]
    config = {"enabled": True, "stock_gap_down_pct": 2.0, "stock_price_drift_pct": 1.5,
              "spy_gap_down_pct": 1.0, "vix_absolute_max": 30, "vix_spike_pct": 20,
              "stock_gap_up_pct": 5.0}

    fake_snapshots = {
        "AAPL": {"current_price": 150.5, "prior_close": 150.0},
        "SPY": {"current_price": 500.0, "prior_close": 499.0},
    }
    with patch("scorched.circuit_breaker.fetch_gate_data", new=AsyncMock(return_value={
        "AAPL": {"current": Decimal("150.5"), "prior_close": Decimal("150.0")},
        "SPY": {"current": Decimal("500.0"), "prior_close": Decimal("499.0")},
        "^VIX": {"current": Decimal("18.0"), "prior_close": Decimal("17.5")},
    })):
        result = await run_circuit_breaker(recs, config)
    assert result[0]["gate_result"].passed is True


@pytest.mark.asyncio
async def test_circuit_breaker_blocks_gap_up():
    """Audit M3: gap_up_gate must actually run."""
    recs = [{"symbol": "AAPL", "action": "buy", "suggested_price": 150.0}]
    config = {"enabled": True, "stock_gap_down_pct": 2.0, "stock_price_drift_pct": 1.5,
              "spy_gap_down_pct": 1.0, "vix_absolute_max": 30, "vix_spike_pct": 20,
              "stock_gap_up_pct": 5.0}

    with patch("scorched.circuit_breaker.fetch_gate_data", new=AsyncMock(return_value={
        "AAPL": {"current": Decimal("160.0"), "prior_close": Decimal("150.0")},  # +6.7% gap
        "SPY": {"current": Decimal("500.0"), "prior_close": Decimal("499.0")},
        "^VIX": {"current": Decimal("18.0"), "prior_close": Decimal("17.5")},
    })):
        result = await run_circuit_breaker(recs, config)
    assert result[0]["gate_result"].passed is False
    assert "gap_up" in result[0]["gate_result"].reason.lower()
```

- [ ] **Step 8.2: Verify failure**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_circuit_breaker.py -v`
Expected: gap-up test FAILS (not wired); Alpaca snapshot test may pass-through depending on existing patch.

- [ ] **Step 8.3: Rewrite `fetch_gate_data` to use Alpaca**

Replace `fetch_gate_data` in `src/scorched/circuit_breaker.py:117-146` with:

```python
async def fetch_gate_data(symbols: list[str]) -> dict:
    """Fetch live prices for circuit breaker checks via Alpaca snapshots.

    For ^VIX (not on Alpaca), falls back to VXX ETF as proxy, then yfinance.
    Records implicit `fetched_at` via timestamp on the data dict; callers
    can fail closed if older than ~5 minutes.
    """
    from .services.alpaca_data import fetch_snapshots_sync

    equity_symbols = list({s for s in symbols if not s.startswith("^")} | {"SPY"})

    loop = asyncio.get_running_loop()
    snaps = await loop.run_in_executor(None, fetch_snapshots_sync, equity_symbols)

    out: dict = {}
    for sym, snap in snaps.items():
        out[sym] = {
            "current": Decimal(str(snap.get("current_price", 0))),
            "prior_close": Decimal(str(snap.get("prior_close", 0))),
        }

    # VIX: try yfinance first, then VXX as proxy.
    try:
        import yfinance as yf
        vix_hist = yf.Ticker("^VIX").history(period="5d")
        if len(vix_hist) >= 2:
            out["^VIX"] = {
                "current": Decimal(str(vix_hist["Close"].iloc[-1])),
                "prior_close": Decimal(str(vix_hist["Close"].iloc[-2])),
            }
    except Exception:
        pass
    if "^VIX" not in out:
        try:
            vxx_snap = await loop.run_in_executor(None, fetch_snapshots_sync, ["VXX"])
            if "VXX" in vxx_snap:
                out["^VIX"] = {
                    "current": Decimal(str(vxx_snap["VXX"].get("current_price", 0))),
                    "prior_close": Decimal(str(vxx_snap["VXX"].get("prior_close", 0))),
                }
        except Exception:
            pass

    return out
```

- [ ] **Step 8.4: Wire `check_gap_up_gate` into `run_circuit_breaker`**

In the per-rec loop inside `run_circuit_breaker` (around lines 183–199), after the existing `check_stock_gate` call, add:

```python
        # Check gap-up — only meaningful for buys.
        if rec["action"] == "buy" and rec["gate_result"].passed:
            gap_up = check_gap_up_gate(
                symbol=rec["symbol"],
                current_price=sym_data.get("current", Decimal("0")),
                prior_close=sym_data.get("prior_close", Decimal("0")),
                config=config,
            )
            if not gap_up.passed:
                rec["gate_result"] = gap_up
```

- [ ] **Step 8.5: Verify**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_circuit_breaker.py -v`
Expected: green.

- [ ] **Step 8.6: Commit**

```bash
git add src/scorched/circuit_breaker.py tests/test_circuit_breaker.py
git commit -m "fix(circuit-breaker): use Alpaca snapshots + wire gap-up gate

Closes audit M2/M3. Replaces yfinance daily history with Alpaca snapshots
(VXX as VIX fallback when yfinance ^VIX is unavailable). check_gap_up_gate
is now actually called during run_circuit_breaker for buy recommendations."
```

---

## Task 9 — Auth fail-closed in every mutation-enabled mode + read-endpoint auth + onboarding lockdown

**Why:** Audit C2/C3/M1/L6. `require_owner_pin` no-ops when PIN unset; sensitive read endpoints have no auth at all; onboarding can write `.env` without auth.

**Files:**
- Modify: `src/scorched/main.py:23-43` (`_assert_live_mode_safe` -> `_assert_auth_safe`, fail in any mode)
- Modify: `src/scorched/api/deps.py` (no-op path removed; require PIN)
- Modify: `src/scorched/api/onboarding.py` (one-shot bootstrap token + post-setup lockout)
- Modify: `src/scorched/api/{recommendations,system,portfolio,playbook,broker_status,strategy}.py` GET endpoints (add `Depends(require_owner_pin)`)
- Modify: `src/scorched/config.py` (add `bootstrap_token` field, `onboarding_completed` flag persisted on disk or env)
- Test: `tests/test_onboarding_auth.py` (fix existing failing tests), `tests/test_auth_failclosed.py` (NEW)

- [ ] **Step 9.1: Failing test for fail-closed startup**

Create `tests/test_auth_failclosed.py`:

```python
"""Audit C2: missing PIN must fail startup in any mode that exposes mutations."""
import pytest

from scorched.main import _assert_auth_safe


def test_paper_mode_requires_pin(monkeypatch):
    monkeypatch.setattr("scorched.main.settings.broker_mode", "paper")
    monkeypatch.setattr("scorched.main.settings.settings_pin", "")
    with pytest.raises(RuntimeError, match="SETTINGS_PIN"):
        _assert_auth_safe()


def test_alpaca_paper_mode_requires_pin(monkeypatch):
    monkeypatch.setattr("scorched.main.settings.broker_mode", "alpaca_paper")
    monkeypatch.setattr("scorched.main.settings.settings_pin", "")
    with pytest.raises(RuntimeError, match="SETTINGS_PIN"):
        _assert_auth_safe()


def test_alpaca_live_requires_long_pin(monkeypatch):
    monkeypatch.setattr("scorched.main.settings.broker_mode", "alpaca_live")
    monkeypatch.setattr("scorched.main.settings.settings_pin", "short")
    with pytest.raises(RuntimeError, match="too short"):
        _assert_auth_safe()


def test_paper_mode_passes_with_short_pin(monkeypatch):
    monkeypatch.setattr("scorched.main.settings.broker_mode", "paper")
    monkeypatch.setattr("scorched.main.settings.settings_pin", "1234")
    _assert_auth_safe()  # any non-empty PIN ok in paper mode
```

- [ ] **Step 9.2: Verify failure**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_auth_failclosed.py -v`
Expected: 4 fails — `_assert_auth_safe` doesn't exist yet (only `_assert_live_mode_safe`).

- [ ] **Step 9.3: Replace `_assert_live_mode_safe` with `_assert_auth_safe`**

In `src/scorched/main.py:23-43`, replace the function:

```python
MIN_LIVE_PIN_LEN = 16


def _assert_auth_safe() -> None:
    """Refuse to boot without a PIN in any mode that exposes mutation endpoints.

    Live mode additionally requires a strong PIN (>=16 chars) and the
    LIVE_TRADING_ENABLED kill switch (Task 10).
    """
    pin = settings.settings_pin or ""
    if not pin:
        raise RuntimeError(
            "SETTINGS_PIN is unset — refusing to start. "
            "Mutation endpoints would be open. Set SETTINGS_PIN in .env."
        )
    if settings.broker_mode == "alpaca_live":
        if len(pin) < MIN_LIVE_PIN_LEN:
            raise RuntimeError(
                f"SETTINGS_PIN too short (len {len(pin)}) for alpaca_live — "
                f"need at least {MIN_LIVE_PIN_LEN} characters"
            )
```

Update the call inside `lifespan`:

```python
    _assert_auth_safe()
```

- [ ] **Step 9.4: Verify startup tests pass**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_auth_failclosed.py tests/test_startup_assertion.py -v`
Expected: green. Update `test_startup_assertion.py` if the previous test relied on the live-only assertion.

- [ ] **Step 9.5: Tighten `require_owner_pin` to require PIN-set**

In `src/scorched/api/deps.py`, replace the function:

```python
def require_owner_pin(x_owner_pin: str = Header(default="")):
    """Guard for sensitive endpoints. Requires X-Owner-Pin header to match SETTINGS_PIN.

    SETTINGS_PIN must be configured at startup — `_assert_auth_safe` enforces this.
    No fail-open path here.
    """
    pin = settings.settings_pin or ""
    if not pin:
        raise HTTPException(status_code=503, detail="Server misconfigured: SETTINGS_PIN unset")
    if not hmac.compare_digest(x_owner_pin, pin):
        raise HTTPException(status_code=403, detail="Incorrect PIN")
```

- [ ] **Step 9.6: Add auth to read endpoints**

For each of these files, add `Depends(require_owner_pin)` to the route decorator's `dependencies=[]` list — they currently have no auth on GET routes:

- `src/scorched/api/recommendations.py:26-54` (GET `/`, GET `/sessions`)
- `src/scorched/api/system.py:19-86` (GET `/health`, GET `/errors`)
- `src/scorched/api/portfolio.py` (GETs except none — verify)
- `src/scorched/api/playbook.py` (GETs)
- `src/scorched/api/broker_status.py` (GET `/status`)

Pattern to apply:

```python
from .deps import require_owner_pin
# ...
@router.get("/...", dependencies=[Depends(require_owner_pin)])
```

Leave only `/healthz` (or equivalent minimal-public liveness route) unauthenticated. Confirm with:

Run: `grep -rn "@router\.\(get\|post\|put\|delete\)" /home/ubuntu/tradebot/src/scorched/api/ | grep -v "Depends(require_owner_pin)"`

The remaining public routes should be only health/readiness checks. If anything sensitive is uncovered, add auth.

- [ ] **Step 9.7: Onboarding bootstrap token**

In `src/scorched/config.py`, add a field (around line 34):

```python
bootstrap_token: str = ""  # one-shot — required for onboarding routes; cleared after first save
onboarding_completed_path: str = "/app/logs/.onboarding_completed"  # presence file
```

In `src/scorched/api/onboarding.py`, replace the existing imports near the top:

```python
import os
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from ..config import settings
```

Add a helper:

```python
def _onboarding_open() -> bool:
    return not Path(settings.onboarding_completed_path).exists()


def require_bootstrap_token(x_bootstrap_token: str = Header(default="")):
    """Onboarding routes require BOOTSTRAP_TOKEN header until first save completes."""
    if not _onboarding_open():
        raise HTTPException(status_code=410, detail="Onboarding already completed; route disabled")
    expected = settings.bootstrap_token
    if not expected:
        raise HTTPException(status_code=503, detail="BOOTSTRAP_TOKEN unset on server")
    import hmac
    if not hmac.compare_digest(x_bootstrap_token, expected):
        raise HTTPException(status_code=403, detail="Incorrect bootstrap token")
```

Replace `Depends(require_owner_pin)` with `Depends(require_bootstrap_token)` on the onboarding routes:
- `POST /validate-key` (line 178)
- `POST /save` (line 271)
- `GET /status` (line 310)

After the `/save` route successfully writes `.env`, mark onboarding complete:

```python
    Path(settings.onboarding_completed_path).touch(exist_ok=True)
```

- [ ] **Step 9.8: Update onboarding-auth tests**

In `tests/test_onboarding_auth.py`, the 3 failing tests previously expected 403 but received 200/422. After this task:
- Without `X-Bootstrap-Token`: 503 (token unset) or 403 (wrong token).
- With completed sentinel file: 410.

Update assertions to match. Re-run tests with bootstrap token mocked:

```python
def test_save_rejects_without_bootstrap_token(monkeypatch, ...):
    monkeypatch.setattr("scorched.config.settings.bootstrap_token", "tok123")
    # call without header -> 403
    # call with wrong header -> 403
    # call with correct header -> proceeds
```

(Mirror the existing test patterns; replace assertions for the new flow.)

- [ ] **Step 9.9: Run full auth suite**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test SETTINGS_PIN=test123 python3 -m pytest tests/test_auth_failclosed.py tests/test_onboarding_auth.py tests/test_startup_assertion.py -v`
Expected: green.

- [ ] **Step 9.10: Commit**

```bash
git add src/scorched/main.py src/scorched/api/deps.py src/scorched/api/onboarding.py src/scorched/api/recommendations.py src/scorched/api/system.py src/scorched/api/portfolio.py src/scorched/api/playbook.py src/scorched/api/broker_status.py src/scorched/config.py tests/test_auth_failclosed.py tests/test_onboarding_auth.py
git commit -m "fix(auth): fail-closed PIN in any mutation mode + read auth + onboarding lockdown

Closes audit C2/C3/M1/L6. Server refuses to boot without SETTINGS_PIN
in any broker mode that exposes mutations. require_owner_pin no longer
no-ops. Sensitive read endpoints (recommendations, system, portfolio,
playbook, broker status) require auth. Onboarding routes require a
separate one-shot BOOTSTRAP_TOKEN and self-disable after first save."
```

---

## Task 10 — `LIVE_TRADING_ENABLED` second kill switch

**Why:** Decision D5. Live mode requires *both* `BROKER_MODE=alpaca_live` AND `LIVE_TRADING_ENABLED=true`. Belt-and-suspenders against accidental live-mode flips.

**Files:**
- Modify: `src/scorched/config.py` (add `live_trading_enabled: bool = False`)
- Modify: `src/scorched/main.py` (`_assert_auth_safe` checks both)
- Modify: `src/scorched/broker/__init__.py` or wherever `get_broker` decides — add same check
- Test: `tests/test_auth_failclosed.py`

- [ ] **Step 10.1: Failing test**

Append to `tests/test_auth_failclosed.py`:

```python
def test_alpaca_live_requires_kill_switch(monkeypatch):
    """Decision D5: BROKER_MODE=alpaca_live without LIVE_TRADING_ENABLED=true must refuse."""
    monkeypatch.setattr("scorched.main.settings.broker_mode", "alpaca_live")
    monkeypatch.setattr("scorched.main.settings.settings_pin", "x" * 16)
    monkeypatch.setattr("scorched.main.settings.live_trading_enabled", False)
    with pytest.raises(RuntimeError, match="LIVE_TRADING_ENABLED"):
        _assert_auth_safe()


def test_alpaca_live_passes_with_both_set(monkeypatch):
    monkeypatch.setattr("scorched.main.settings.broker_mode", "alpaca_live")
    monkeypatch.setattr("scorched.main.settings.settings_pin", "x" * 16)
    monkeypatch.setattr("scorched.main.settings.live_trading_enabled", True)
    _assert_auth_safe()
```

- [ ] **Step 10.2: Verify failure**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_auth_failclosed.py::test_alpaca_live_requires_kill_switch -v`
Expected: AttributeError — `live_trading_enabled` not in settings.

- [ ] **Step 10.3: Add config field**

In `src/scorched/config.py`, near the broker_mode field, add:

```python
live_trading_enabled: bool = False  # second kill switch for live trading (decision D5)
```

- [ ] **Step 10.4: Add the assertion**

In `src/scorched/main.py:_assert_auth_safe`, after the live-mode PIN-length check, add:

```python
    if settings.broker_mode == "alpaca_live":
        if len(pin) < MIN_LIVE_PIN_LEN:
            raise RuntimeError(...)  # existing
        if not settings.live_trading_enabled:
            raise RuntimeError(
                "BROKER_MODE=alpaca_live but LIVE_TRADING_ENABLED is not true — "
                "refusing to start. Set LIVE_TRADING_ENABLED=true in .env to enable live trading."
            )
```

- [ ] **Step 10.5: Add a runtime assertion in the broker factory**

In `src/scorched/broker/__init__.py` (or whichever module hosts `get_broker`), inspect for the live-mode branch. Add at the top of that branch:

```python
    if settings.broker_mode == "alpaca_live" and not settings.live_trading_enabled:
        raise RuntimeError("Cannot construct AlpacaBroker in live mode without LIVE_TRADING_ENABLED=true")
```

Find the right location:

Run: `grep -n "alpaca_live\|AlpacaBroker" /home/ubuntu/tradebot/src/scorched/broker/__init__.py /home/ubuntu/tradebot/src/scorched/broker/alpaca.py`

Place the check inside `get_broker` so a runtime flip can't bypass startup.

- [ ] **Step 10.6: Run all auth tests**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test python3 -m pytest tests/test_auth_failclosed.py -v`
Expected: green.

- [ ] **Step 10.7: Run the full suite once more**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test SETTINGS_PIN=test123 python3 -m pytest -q`
Expected: pass count ≥ pre-Tier-1 baseline. Any new failures must be diagnosed.

- [ ] **Step 10.8: Commit**

```bash
git add src/scorched/config.py src/scorched/main.py src/scorched/broker/__init__.py tests/test_auth_failclosed.py
git commit -m "feat(safety): LIVE_TRADING_ENABLED kill switch for alpaca_live

Decision D5. Live mode now requires both BROKER_MODE=alpaca_live AND
LIVE_TRADING_ENABLED=true. Enforced at startup and at broker
construction. Belt-and-suspenders against accidental live flips."
```

---

## Post-Tier-1 verification

After Task 10:

- [ ] **Step V.1: Run full pytest suite**

Run: `cd /home/ubuntu/tradebot && ANTHROPIC_API_KEY=test SETTINGS_PIN=test123 python3 -m pytest -q`
Expected: green or only previously-known unrelated failures.

- [ ] **Step V.2: Run guidance lint**

Run: `cd /home/ubuntu/tradebot && python3 -m scorched.services.guidance_lint`
Expected: 0 errors.

- [ ] **Step V.3: Run strategy doc lint**

Run: `cd /home/ubuntu/tradebot && python3 scripts/check_strategy_docs.py`
Expected: pass.

- [ ] **Step V.4: Rebuild Docker image and confirm boot**

Run: `cd /home/ubuntu/tradebot && docker compose up -d --build tradebot`
Then: `docker compose logs tradebot --tail=50`
Expected: container running, no `RuntimeError` at startup, `_assert_auth_safe` passed because real `.env` has `SETTINGS_PIN` set.

- [ ] **Step V.5: Hit `/api/v1/system/health` with PIN to confirm read auth works**

Run: `curl -H "X-Owner-Pin: $SETTINGS_PIN" http://localhost:8000/api/v1/system/health` (or via Tailscale IP).
Expected: 200 with health JSON.
Run: `curl http://localhost:8000/api/v1/system/health`
Expected: 403.

- [ ] **Step V.6: Update memory + project notes**

Append a note to `~/.claude/projects/-home-ubuntu-tradebot/memory/MEMORY.md` summarising Tier 1 completion (date, commits, audit findings closed). Do NOT write the full content into MEMORY.md — create a dedicated memory file (e.g. `project_tier1_audit_remediation.md`) and link it from MEMORY.md per the auto-memory protocol.

---

## Out of Scope (deferred to subsequent plans)

- **Tier 2 — Reliability hardening:** H7 (Phase 0 exception handling), H8 (recommendation generation lock), H9 (Phase 2 retry retention), M4 (deterministic hard-rule validators), M5/M6 (bar hygiene), M7 (REST/MCP confirm parity), M9 (reconciliation atomicity). Separate plan.
- **Tier 3 — Cleanup:** L1 (stale phase scripts), L2 (Polygon leftovers), L3/L4 (paths/IPs to settings), L5 (real broker health), M12 (settings injectability + onboarding test fixes). Separate plan.
- **Tier 4 — Strategy validation:** Walk-forward backtest harness, replay-last-90-days minimum (audit M13). Separate plan; multi-week effort.
- **D2 follow-through:** Trailing-stop alert-on-every-breach (Telegram even when Claude says hold). Small enough to fold into Tier 2; not gating live mode.
