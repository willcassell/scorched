# Best-in-Class Upgrade Implementation Plan (Experiment C)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade Scorched to Claude Opus 5, fix all 7 known bugs/contradictions, add the four missing strategy systems (exposure management, exit telemetry, re-entry cooldown, mechanical entry gate), and reset the kill-criterion test as Experiment C.

**Architecture:** All Claude calls are centralized in `src/scorched/services/claude_client.py` (constants `MODEL`, `HAIKU_MODEL`, `THINKING_BUDGET`), so the model migration is one file plus cost tables. New strategy systems follow the existing code-gate pattern (`check_*` functions in `recommender.py`/`risk_gates.py` that log `gate_decisions` rows). Exit telemetry adds two nullable columns to `trade_history` threaded through the broker layer.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async + Alembic, `anthropic` AsyncAnthropic SDK, pytest, Docker Compose. DB is Postgres in the `tradebot-postgres-1` container.

## Global Constraints

- Model target: `claude-opus-5` for ALL calls (no more sonnet/haiku split). Pricing $5/$25 per MTok.
- Opus 5 API rules: `budget_tokens` returns 400 — never send it. Thinking is ON by default (omitting `thinking` = adaptive). `thinking: {"type": "disabled"}` allowed only at effort `high` or below. `temperature`/`top_p`/`top_k` are rejected — never send. Effort goes in `output_config={"effort": ...}`. `max_tokens` caps thinking + text together.
- Always check `response.stop_reason == "refusal"` before parsing content.
- All new numeric knobs live in `strategy.json` with code fallback defaults matching the shipped values.
- New code gates MUST log `gate_decisions` rows (existing pattern: see `check_factor_alignment` in `recommender.py`).
- After each task: run `docker compose exec -T tradebot python -m pytest tests/ -q` (or local venv pytest if faster) — suite must stay green (400 tests at start).
- Conventional commits. One commit per task.
- NEVER use `date.today()` — use `market_today()` from `src/scorched/tz.py`.
- Keep `analyst_guidance.md`, `strategy.md`, `src/scorched/CLAUDE.md`, and `services/playbook.py:_DRIFT_PATTERNS` in sync with any strategy.json change (user's standing rule).

---

### Task 1: Opus 5 model migration (claude_client.py + cost.py)

**Files:**
- Modify: `src/scorched/services/claude_client.py` (constants at ~line 142, call sites at ~296-520)
- Modify: `src/scorched/cost.py` (`_PRICING` dict, line 16)
- Modify: `src/scorched/services/eod_review.py` (imports `HAIKU_MODEL`, line 195)
- Test: `tests/` — existing tests referencing model names/fixtures

**Interfaces:**
- Produces: `MODEL = "claude-opus-5"`; new dict `EFFORT = {"analysis": "xhigh", "decision": "high", "risk_review": "high", "position_mgmt": "medium", "intraday_exit": "high", "playbook": "medium", "reflection": "medium", "quick": "low"}`; helper `def _refusal_guard(response) -> None` raising `ClaudeRefusalError` (new exception class in same file).
- `HAIKU_MODEL` is deleted; all former haiku call sites use `MODEL`.

- [ ] **Step 1: Write failing test**

```python
# tests/test_model_migration.py
from scorched.services import claude_client
from scorched import cost

def test_model_is_opus5():
    assert claude_client.MODEL == "claude-opus-5"
    assert not hasattr(claude_client, "HAIKU_MODEL")

def test_no_budget_tokens_anywhere():
    import inspect
    src = inspect.getsource(claude_client)
    assert "budget_tokens" not in src

def test_opus5_pricing():
    assert cost._PRICING["claude-opus-5"] == (5.0, 25.0, 5.0)

def test_refusal_guard_raises():
    import pytest
    class R: stop_reason = "refusal"; stop_details = None
    with pytest.raises(claude_client.ClaudeRefusalError):
        claude_client._refusal_guard(R())
```

- [ ] **Step 2: Run → verify FAIL** (`pytest tests/test_model_migration.py -q`)

- [ ] **Step 3: Implement**

In `claude_client.py`:
```python
MODEL = "claude-opus-5"

# Per-call reasoning effort (Opus 5 adaptive thinking is always on; effort is the depth lever)
EFFORT = {
    "analysis": "xhigh",
    "decision": "high",
    "risk_review": "high",
    "position_mgmt": "medium",
    "intraday_exit": "high",
    "playbook": "medium",
    "reflection": "medium",
    "quick": "low",
}

class ClaudeRefusalError(RuntimeError):
    """Opus 5 safety classifiers declined the request (stop_reason == 'refusal')."""

def _refusal_guard(response) -> None:
    if getattr(response, "stop_reason", None) == "refusal":
        detail = getattr(response, "stop_details", None)
        raise ClaudeRefusalError(f"Claude refused request: {detail}")
```

Edit every `client.messages.create(...)` call site:
- **Call 1 (analysis, ~line 296):** delete `thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET}`; replace with `thinking={"type": "adaptive", "display": "summarized"}` (keeps `extract_thinking` logging working — Opus 5 default display is `omitted` = empty text) and `output_config={"effort": EFFORT["analysis"]}`. Keep `max_tokens=THINKING_BUDGET + 4096` (20480 — now the combined thinking+text cap; adaptive typically uses less).
- **Call 2/3 (decision/risk, `max_tokens=2048`):** raise `max_tokens` to `8192` (adaptive thinking shares the cap), add `output_config={"effort": EFFORT["decision"]}` / `EFFORT["risk_review"]`, `thinking={"type": "adaptive", "display": "summarized"}`.
- **Fix-up retry calls (~321, ~394):** `thinking={"type": "disabled"}`, `output_config={"effort": "low"}`, `max_tokens=2048` (disabled is legal at effort ≤ high).
- **Former HAIKU call sites (~464 intraday exit, ~500 quick):** `model=MODEL`; intraday exit gets `output_config={"effort": EFFORT["intraday_exit"]}`, `max_tokens=4096`; quick call gets `effort: "low"`, `thinking={"type": "disabled"}`, `max_tokens=1024`.
- **Playbook (~482), reflection (`reflection.py:216`), eod (`eod_review.py:195, 252`):** `model=MODEL`, effort per `EFFORT` map, `max_tokens=8192` where currently 2048, thinking adaptive+summarized.
- After every `create()` call, insert `_refusal_guard(response)` before any content parsing.
- `THINKING_BUDGET` constant: keep the name (used for max_tokens arithmetic) but update its comment to say it's a max_tokens headroom number, not an API thinking budget.

In `cost.py` `_PRICING`, add: `"claude-opus-5": (5.0, 25.0, 5.0),` and keep old entries (historical rows still reference them). Change `_DEFAULT_PRICING` to `(5.0, 25.0, 5.0)`.

In `eod_review.py`, change `from .claude_client import ... HAIKU_MODEL` imports to use `MODEL` only.

- [ ] **Step 4: Run full suite → PASS** (`pytest tests/ -q`; fix any fixtures asserting old model strings)

- [ ] **Step 5: Commit** — `feat(models): migrate all Claude calls to claude-opus-5 with adaptive thinking + effort map`

---

### Task 2: Sync stale prompt file `src/scorched/CLAUDE.md`

**Files:**
- Modify: `src/scorched/CLAUDE.md`

This file is injected into prompts each call and still contains pre-Experiment-B rules that contradict live config.

- [ ] **Step 1:** Read the file. Delete/replace: (a) Step 4 Exit Checklist rows "+15% gain → sell 50%" and "+25% gain → sell remainder" → replace with "Exits are trailing-stop driven (HWM − 2×ATR, −5% floor); no fixed profit targets; `partial_sell: never`"; (b) Step 3 sizing table entries allowing "10–20% of portfolio" → cap all rows at 15% (`max_position_pct`); (c) any "3 trades/day" or mean-reversion-entry language → breakout-only per experiment.
- [ ] **Step 2:** Run `python scripts/check_strategy_docs.py` and `python scripts/guidance_lint.py` — both clean.
- [ ] **Step 3:** Commit — `docs(prompts): sync src/scorched/CLAUDE.md exit/sizing rules with live strategy`

---

### Task 3: Exit-reason telemetry (migration + threading)

**Files:**
- Create: `alembic/versions/<autogen>_add_exit_reason_to_trade_history.py` (via `alembic revision --autogenerate`)
- Modify: `src/scorched/models.py` (TradeHistory), `src/scorched/broker/base.py` + `paper.py` + `alpaca.py` (`submit_sell` signature), `src/scorched/broker/pending_fills.py` (record field), `src/scorched/services/reconciliation.py` + `services/portfolio.py` (`apply_sell`), `src/scorched/api/intraday.py` (hard-stop + Claude exits), `src/scorched/api/trades.py` (confirm endpoint passes "recommendation")
- Test: `tests/test_exit_telemetry.py`

**Interfaces:**
- Produces: `TradeHistory.exit_reason: str | None` (varchar(40)) and `TradeHistory.exit_trigger: str | None` (varchar(40)). `submit_sell(..., exit_reason: str | None = None, exit_trigger: str | None = None)` on all brokers; pending-fill records carry both; `apply_sell(..., exit_reason=None, exit_trigger=None)` persists them.
- Reason vocabulary: `"recommendation"` (Phase 2 sell), `"intraday_hard_stop"`, `"intraday_claude_exit"`, `"manual"`. Trigger vocabulary = the 6 intraday trigger names (`position_drop_from_entry`, `position_drop_from_open`, `spy_intraday_drop`, `vix_above_threshold`, `volume_surge`, `trailing_stop_breached`) or None.

- [ ] **Step 1: Failing test**

```python
# tests/test_exit_telemetry.py — follow existing apply_sell test fixtures in tests/
async def test_apply_sell_persists_exit_reason(db_session, seeded_position):
    await apply_sell(db_session, symbol="AAPL", shares=..., price=...,
                     exit_reason="intraday_hard_stop", exit_trigger="position_drop_from_entry")
    row = (await db_session.execute(
        select(TradeHistory).where(TradeHistory.action == "sell"))).scalar_one()
    assert row.exit_reason == "intraday_hard_stop"
    assert row.exit_trigger == "position_drop_from_entry"
```

- [ ] **Step 2:** Run → FAIL. Add columns to `models.py`, generate + apply alembic migration (nullable, no backfill).
- [ ] **Step 3:** Thread the two kwargs: `api/intraday.py` `_execute_emergency_sell` passes `exit_reason="intraday_hard_stop"`, `exit_trigger=trigger.trigger_type`; the Claude-exit sell path passes `"intraday_claude_exit"` + trigger; `api/trades.py` confirm passes `"recommendation"`; broker `submit_sell` writes both into the pending-fill record; `reconcile_pending_orders` reads them back into `apply_sell`. PaperBroker passes straight through.
- [ ] **Step 4:** Full suite PASS; `alembic upgrade head` clean in container.
- [ ] **Step 5:** Commit — `feat(telemetry): persist exit_reason/exit_trigger on every sell`

---

### Task 4: Phase 2 execution gap — DB is authority + stale-rec cleanup

**Files:**
- Modify: `cron/tradebot_phase2.py` (~lines 55-100), `src/scorched/api/recommendations.py` (ensure GET can filter today+pending)
- Test: `tests/test_phase2_db_authority.py` (unit-test the merge helper — extract it as a pure function)

**Context:** Phase 2 executes only what's in the Phase 1/1.5 JSON file. Three risk-review-cleared buys (ABBV 6/23, GS 7/14, AMZN 7/31) had no Phase 2 confirm attempt — the file pipeline silently dropped them. DB `trade_recommendations` is the source of truth.

- [ ] **Step 1:** Extract a pure function in `cron/tradebot_phase2.py`:

```python
def merge_pending(file_recs: list[dict], db_recs: list[dict]) -> tuple[list[dict], list[str]]:
    """Union file recs with today's DB-pending recs. Returns (merged, missing_from_file_symbols).
    DB rec shape comes from GET /api/v1/recommendations (id, symbol, action, status, suggested_price...).
    Only status == 'pending' DB recs are added; file entries win on duplicates (they carry gate results)."""
    by_symbol = {(r["symbol"], r["action"]): r for r in file_recs}
    missing = []
    merged = list(file_recs)
    for r in db_recs:
        if r.get("status") != "pending":
            continue
        key = (r["symbol"], r["action"])
        if key not in by_symbol:
            merged.append(r)
            missing.append(f"{r['action']} {r['symbol']}")
    return merged, missing
```

Failing test: file has [buy AAPL], DB has [pending buy AAPL, pending buy GS, rejected buy XOM] → merged has GS, missing == ["buy GS"].
- [ ] **Step 2:** Wire into `main()`: after loading `recs` from file, call `http_get("/api/v1/recommendations")` (today's session), run `merge_pending`, and if `missing` is non-empty prepend a `⚠️ PHASE 2 FILE/DB MISMATCH — executing from DB: ...` line to the Telegram summary. Circuit-breaker results for file recs unchanged; DB-only additions are treated as ungated (they passed Phase 1 filters; note this in the warning).
- [ ] **Step 3:** One-off cleanup (run manually, document in commit message): mark stale pending recs as expired —

```sql
UPDATE trade_recommendations SET status='rejected'
WHERE status='pending' AND created_at < '2026-07-31'
  AND id IN (SELECT id FROM trade_recommendations WHERE symbol IN ('PFE','LLY','GS') AND status='pending');
```

(Verify ids first with a SELECT; PFE 6/18 + LLY 7/14 sells were superseded by intraday auto-exits, GS 7/14 buy expired.)
- [ ] **Step 4:** Suite PASS. Commit — `fix(phase2): execute from DB-pending recs, alert on file/DB mismatch; expire stale recs`

---

### Task 5: Factor gate correctness (fail-closed, true 20d return, ETF list, provenance)

**Files:**
- Modify: `src/scorched/services/recommender.py` (`check_factor_alignment`), `src/scorched/services/guidance.py` (`_PROVENANCE_MAP` rule 9), `analyst_guidance.md` (rule #9 ETF list)
- Test: extend existing factor-gate tests (7 exist from Experiment B)

- [ ] **Step 1: Failing tests:** (a) missing SPY/factor data → gate returns BLOCKED with reason `"factor_data_missing"` (fail-closed, matching sector gate posture) and a Telegram alert is sent (mock `send_telegram`); (b) candidate momentum uses `trailing_20d_return_pct` when present, falling back to `month_change_pct` only with a logged warning; (c) leaders checked = MTUM, SPMO, QQQ (docs updated to match code — do NOT add IWM/RSP to code: IWM/RSP leading is a small-cap/equal-weight regime where breakout momentum entries are still valid; guidance text is the thing that's wrong).
- [ ] **Step 2:** Implement: compute `trailing_20d_return_pct` in `build_research_context()`/Phase 0 from Alpaca bars (`(close[-1]/close[-21]-1)*100` when ≥21 bars) and store per-symbol in `price_data`; `check_factor_alignment` prefers it. Flip fail-open branches to return blocked + reason. Update `_PROVENANCE_MAP["9"]` to `"both"`. Fix `analyst_guidance.md` rule #9 to name MTUM/SPMO/QQQ only.
- [ ] **Step 3:** Suite PASS. Commit — `fix(factor-gate): fail closed, true 20-trading-day momentum, docs/provenance sync`

---

### Task 6: Mean-reversion screener — code-level disable + config-driven

**Files:**
- Modify: `src/scorched/services/recommender.py` (~lines 490-491) and/or `src/scorched/api/prefetch.py` (Phase 0 fetch)
- Test: `tests/test_entry_style_gating.py`

- [ ] **Step 1: Failing test:** with `strategy.json entry_style == ["breakout"]`, the research context builder is not fed mean-reversion candidates (mock `fetch_mean_reversion_screener`, assert not called / results excluded).
- [ ] **Step 2:** Gate both the Phase 0 fetch and the recommender fallback fetch on `"mean_reversion" in strategy.get("entry_style", [])`. The screener code stays (re-enable by editing strategy.json).
- [ ] **Step 3:** Suite PASS. Commit — `fix(strategy): mean-reversion screener gated by entry_style config, not prompt-only`

---

### Task 7: Trailing-stop params configurable + stale sizing fallbacks

**Files:**
- Modify: `src/scorched/trailing_stops.py` (defaults stay 2.0 / 5.0), `src/scorched/api/intraday.py` + `cron/intraday_monitor.py` + `services/portfolio.py` (callers read config), `strategy.json` (add `trailing_stop` section), `src/scorched/services/recommender.py` (lines ~771, ~1084: `max_position_pct` fallback 33 → 15)
- Test: `tests/test_trailing_config.py`

- [ ] **Step 1: Failing test:** `strategy.json` with `{"trailing_stop": {"atr_multiplier": 2.5, "floor_pct": 6.0}}` → stop computed as `max(hwm - 2.5*atr, entry*0.94)`. Plus: `check_position_cap` with missing `max_position_pct` key uses 15, not 33.
- [ ] **Step 2:** Add `strategy.json` section `"trailing_stop": {"atr_multiplier": 2.0, "floor_pct": 5.0}` (values unchanged — this ships config surface, not a behavior change). Thread through callers. Replace both `33` fallbacks with `15`.
- [ ] **Step 3:** Suite PASS; run `python scripts/check_strategy_docs.py`. Commit — `feat(config): trailing-stop params in strategy.json; fix stale 33% sizing fallbacks`

---

### Task 8: Exposure management (advisory target + telemetry)

**Files:**
- Modify: `strategy.json` (new `exposure` section), `src/scorched/services/recommender.py` (context injection + gate row), `analyst_guidance.md` (new hard rule #10), `src/scorched/services/guidance.py` (provenance), `services/playbook.py:_DRIFT_PATTERNS` (no change needed unless numbers named — check)
- Test: `tests/test_exposure.py`

**Design:** Code cannot force good buys, so the floor is advisory-but-loud: a context section + hard rule Claude must answer to, plus a `gate_decisions` telemetry row every session so we can audit "underinvested while regime healthy" days. The ceiling stays enforced by existing position/cash gates.

strategy.json:
```json
"exposure": {
  "target_min_invested_pct": 60,
  "target_max_invested_pct": 90,
  "regime_condition": "spy_above_20dma_and_no_drawdown_gate"
}
```

- [ ] **Step 1: Failing tests** for a pure function in `recommender.py`:

```python
def assess_exposure(invested_pct: float, spy_above_20dma: bool, drawdown_gate_active: bool,
                    cfg: dict) -> dict:
    """Returns {"status": "underinvested"|"in_range"|"overinvested"|"defensive_ok",
                "invested_pct": float, "target_min": float, "target_max": float}"""
```
Cases: 12% invested + SPY>20dma + no gate → `underinvested`; 12% + SPY<20dma → `defensive_ok`; 70% → `in_range`; 95% → `overinvested`.
- [ ] **Step 2:** Implement; in `generate_recommendations()` compute invested pct from portfolio state (`(total_value - cash) / total_value * 100`), SPY vs 20dma from cached technicals/factor data, call `assess_exposure`, write a `gate_decisions` row (`gate="exposure_check"`, `passed=status in ("in_range","defensive_ok")`, `reason=status`, details JSON with numbers). Inject into Phase 1 context ahead of candidates:

```
EXPOSURE STATUS: 11.9% invested (target 60-90% when SPY > 20d MA and drawdown gate clear).
STATUS: UNDERINVESTED — hard rule #10 applies.
```
- [ ] **Step 3:** Add `analyst_guidance.md` hard rule #10 (and provenance entry `"10": "prompt+telemetry"`):

> **10. Exposure discipline.** When EXPOSURE STATUS is UNDERINVESTED (invested % below target floor with SPY above its 20-day MA and the drawdown gate clear), you must either (a) propose enough qualifying breakout buys to move materially toward the target floor, or (b) explicitly list, per vacancy, which entry criterion failed for the best remaining candidate. "No compelling setups" without naming candidates and criteria is not acceptable. Never lower entry standards to fill the target — document the shortfall instead.
- [ ] **Step 4:** Suite PASS. Commit — `feat(strategy): exposure target + underinvestment telemetry and hard rule #10`

---

### Task 9: Re-entry cooldown gate (code-enforced)

**Files:**
- Modify: `src/scorched/services/recommender.py` (new gate in the per-symbol BUY loop, after `factor_alignment`), `strategy.json` (`"reentry_cooldown_days": 3` under a new `entry_gates` section or top-level), `analyst_guidance.md` (document under hard rules)
- Test: `tests/test_reentry_cooldown.py`

- [ ] **Step 1: Failing tests** for:

```python
async def check_reentry_cooldown(db, symbol: str, cooldown_days: int) -> tuple[bool, str]:
    """Blocks a BUY if the symbol has a SELL in trade_history within the last
    `cooldown_days` NYSE trading days (pandas_market_calendars, same helper as circuit breaker).
    Returns (allowed, reason). Fail-open ONLY on DB error (log it)."""
```
Cases: sell 1 trading day ago → blocked (`"reentry_cooldown"`); sell 5 trading days ago (cooldown 3) → allowed; no sells → allowed; weekend gap counted in trading days.
- [ ] **Step 2:** Implement; wire into the BUY gate chain with a `gate_decisions` row (`gate="reentry_cooldown"`). Add `"reentry_cooldown_days": 3` to strategy.json. Document in `analyst_guidance.md` hard-rules table (provenance `"code"`).
- [ ] **Step 3:** Suite PASS. Commit — `feat(gates): re-entry cooldown blocks whipsaw re-buys within 3 trading days of a sell`

---

### Task 10: Mechanical entry gate (screener originates, Claude vetoes)

**Files:**
- Modify: `src/scorched/services/recommender.py` (new `check_mechanical_entry` in BUY gate chain, before risk review so rejects save an LLM argument — actually place in per-symbol loop next to other gates for consistency), `strategy.json` (`"mechanical_entry": {"enabled": true, "min_momentum_5d_pct": 0.0, "min_rel_volume": 1.0, "require_above_20dma": true}`), `analyst_guidance.md` (hard rule #11)
- Test: `tests/test_mechanical_entry.py`

**Design:** Backtest evidence (6/18): LLM-originated entries lose to a mechanical breakout rule. Every BUY must now pass the mechanical minimums *in code* from cached research data; Claude's role narrows to selecting/vetoing among mechanically-qualified names.

- [ ] **Step 1: Failing tests** for:

```python
def check_mechanical_entry(symbol: str, price_data: dict, technicals: dict, cfg: dict) -> tuple[bool, str]:
    """price_data[symbol] carries momentum_5d_pct / rel_volume / above_20dma fields
    (already computed for the research context). Returns (allowed, reason).
    Missing individual field → that criterion passes with a logged warning ONLY if
    cfg.get('fail_open_on_missing', False); default False → blocked with 'mechanical_data_missing'."""
```
Cases: momentum +2%, rel_vol 1.4, above 20dma → allowed; momentum −1% → blocked `"mechanical_momentum"`; rel_vol 0.6 → blocked `"mechanical_volume"`; below 20dma → blocked `"mechanical_trend"`; missing data + default cfg → blocked `"mechanical_data_missing"`.
- [ ] **Step 2:** Implement + wire into BUY chain with `gate_decisions` row (`gate="mechanical_entry"`). Confirm the fields exist in the research-context data structures (they are computed for context rendering — thread them into a per-symbol dict the gate can read; if rel-volume at 9:45 AM is the known-broken 0.0x reading, reuse the fix from commit 997c2af's volume source). Add hard rule #11 text to `analyst_guidance.md`: "Every BUY must already satisfy the mechanical entry minimums (5-day momentum > 0, relative volume ≥ 1.0, price above 20-day MA) — these are code-enforced; your job is selection and veto among qualifying names, not exception-making."
- [ ] **Step 3:** Suite PASS. Commit — `feat(gates): code-enforced mechanical entry minimums for every BUY`

---

### Task 11: Experiment C reset

**Files:**
- Modify: `strategy.json` (`experiment` block), `scripts/evaluate_experiment_b.py` → rename/generalize to `scripts/evaluate_experiment.py` reading baselines from strategy.json, `analyst_guidance.md` (banner), `strategy.md`, `src/scorched/CLAUDE.md` (banner)
- Create: `.handovers/2026-07-31-experiment-C.md`
- Test: `tests/test_evaluate_experiment.py` (if eval script has tests; else `--dry-run` style check)

- [ ] **Step 1:** Capture baseline on ship day (run inside container):

```python
# baseline = portfolio total_value (live prices) + SPY close, written into strategy.json:
"experiment": {
  "name": "C-best-in-class",
  "start_date": "<ship date, e.g. 2026-08-03>",
  "deadline_trading_days": 60,
  "deadline_approx_date": "<start + 60 NYSE days>",
  "baseline_portfolio_value": <total_value at ship>,
  "baseline_spy": <SPY close at ship>,
  "kill_unless": "window_return > SPY_window_return AND window_profit_factor > 1.0",
  "fallback": "retire (turn off crons)",
  "thesis": "Opus 5 + mechanical entry gate + exposure discipline + cooldown + exit telemetry fix the entry-quality and underexposure failures; Experiment B carryovers (breakout-only, factor gate, 15% size, trailing exits) retained."
}
```
- [ ] **Step 2:** Generalize the eval script: read `experiment` block for baselines/dates instead of hardcoded constants; window PF from `trade_history.executed_at >= start_date`. Keep the KEEP/RETIRE verdict format.
- [ ] **Step 3:** Update the experiment banner in `analyst_guidance.md` + `strategy.md` + `src/scorched/CLAUDE.md` to name Experiment C and its rules. Check `_DRIFT_PATTERNS` — add old Experiment B deadline strings if they'd now count as drift. Write `.handovers/2026-07-31-experiment-C.md`: what shipped (Tasks 1-10 summary), baseline numbers, kill procedure, evidence links (this plan + the 7/31 deep-dive findings).
- [ ] **Step 4:** `scripts/check_strategy_docs.py` clean. Commit — `feat(experiment): reset kill-criterion test as Experiment C with config-driven eval`

---

### Task 12: Deploy, live smoke, memory update

- [ ] **Step 1:** `docker compose exec -T tradebot python -m pytest tests/ -q` — full suite green.
- [ ] **Step 2:** `alembic upgrade head` in container → `docker compose up -d --build tradebot` → container healthy; `docker compose logs tradebot --since 2m` clean of tracebacks.
- [ ] **Step 3:** Live smoke: hit `/api/v1/system/market-date`; run one real Opus 5 call cheaply — `docker compose exec -T tradebot python -c` snippet calling the quick/summary helper in `claude_client.py` and printing `response.model` (must start with `claude-opus-5`) and usage. Verify a `token_usage` row lands with the opus-5 cost rate.
- [ ] **Step 4:** Update auto-memory: MEMORY.md model line (opus-5 everywhere), Experiment C entry (baseline, kill date, what shipped), mark Experiment B entry superseded. Update project `CLAUDE.md` (models, new gates, exit telemetry, Phase 2 DB authority) and mirror essentials to `AGENTS.md` if present.
- [ ] **Step 5:** Final commit if anything uncommitted — `docs: sync CLAUDE.md/memory for Experiment C`

---

## Self-Review Notes

- Spec coverage: model upgrade (T1), 7 bugs — stale CLAUDE.md (T2), exit telemetry gap (T3), Phase 2 gap + stuck recs (T4), factor gate fail-open/month-change/ETF-list/provenance (T5), mean-reversion prompt-only suspension (T6), trailing-floor configurability + 33% fallbacks (T7) — missing systems: exposure (T8), cooldown (T9), mechanical entries (T10); Experiment C (T11); deploy+verify (T12). The "trailing −5% inside −8% hard stop" tension is deliberately NOT changed (handover says revisit only if winners get shaken out; T7 makes it tunable without a rebuild).
- Type consistency: `submit_sell` kwargs named `exit_reason`/`exit_trigger` everywhere; gate rows use `gate=` strings `exposure_check`, `reentry_cooldown`, `mechanical_entry`, factor gate reason `factor_data_missing`.
- Placeholders: none — every step names files, code, and expected test outcomes.
