# 2026-04-17 Audit Remediation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the 16 highest-impact findings from the 2026-04-17 deep-review audit — re-activate safeguards that are advertised but silently disabled, eliminate wasted data fetches, tighten the Tailscale-only security posture, and improve failure observability.

**Architecture:** Each task is a narrow, self-contained code/config change with its own test (where testable) and its own commit. Tasks are grouped by subsystem but can be executed in any order unless explicitly marked as dependent. The plan works directly on `main` — no worktree.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy async, Alpaca SDK, pytest, Docker Compose. Container rebuild pattern: `docker compose up -d --build tradebot`.

---

## Context snapshot (gaps being closed)

This plan fixes the 16 P0/P1 items from the audit. Each task below starts with a **Gap** section describing the current broken state so this document also serves as a record of what was wrong.

| # | Title | Area | Severity |
|---|-------|------|----------|
| 1 | Risk committee filter is theater | Strategy | 🔴 |
| 2 | Circuit breaker silently disabled (wrong cwd) | Timing | 🔴 |
| 3 | `.env` world-readable + 4-digit PIN | Security | 🟠 |
| 4 | `/api/v1/onboarding/*` ungated | Security | 🟠 |
| 5 | PID locks don't detect hung processes | Timing | 🔴 |
| 6 | Twelvedata RSI + economic calendar fetched but never shown to Claude | Strategy | 🟠 |
| 7 | Alpaca news hardcoded to empty dict in Phase 0 | Strategy | 🟠 |
| 8 | Trailing stops are dead code | Strategy | 🔴 |
| 9 | `max_sector_pct` is prompt-only, never enforced | Strategy | 🟠 |
| 10 | `strategy.json` contradicts `analyst_guidance.md` | Strategy | 🔴 |
| 11 | `week_change_pct` scoring uses `abs()` | Strategy | 🟠 |
| 12 | `portfolio.total_value` uses cost basis, not live prices | Strategy | 🟠 |
| 13 | Phase 0 `asyncio.gather` has no timeout | Timing | 🟠 |
| 14 | Phase 3 EOD review fails silently | Timing | 🟠 |
| 15 | API keys leak into `api_call_log` error messages | Security | 🟡 |
| 16 | No startup assertion for live-mode + weak PIN | Security | 🟡 |

**Suggested execution order:** 10 → 3 → 4 → 16 → 15 → 2 → 5 → 1 → 11 → 12 → 6 → 7 → 8 → 9 → 13 → 14.

Rationale: do the user-decision task (10) first so downstream strategy work is consistent; then cheap security wins (3, 4, 16, 15); then re-enable the gate paths (2, 5); then the big safeguard fixes (1, 8, 9); then data plumbing (6, 7); then observability (13, 14).

---

## Task 1: Fix RiskDecisionEntry missing `action` field

### Gap

The risk committee's JSON output includes `action` ("buy" or "sell") per decision — the prompt at `src/scorched/prompts/risk_review.md:18` explicitly asks for it and Claude complies. But `RiskDecisionEntry` at `src/scorched/services/claude_client.py:107-110` only captures `symbol`, `verdict`, `reason`. After Pydantic validation + `model_dump()`, `action` is dropped.

Downstream in `src/scorched/services/recommender.py:549-553`:

```python
rejected_symbols = {
    d["symbol"].upper()
    for d in risk_decisions
    if d.get("verdict") == "reject" and d.get("action", "").lower() == "buy"
}
```

Because `action` is always missing, this filter always evaluates empty. Every "reject" verdict is logged but never removes a buy. The only time filtering actually happens is on parse failure (fail-closed at line 547) — i.e., the bug only *doesn't* bite when the parser fails entirely.

### Files
- Modify: `src/scorched/services/claude_client.py` (add `action` field to `RiskDecisionEntry`)
- Modify: `tests/test_risk_review.py` (add test asserting action flows through)
- Test: `tests/test_claude_client.py` (add test for `RiskReviewOutput` round-trip)

### Steps

- [ ] **Step 1: Write the failing test for `RiskDecisionEntry.action`**

Edit `tests/test_claude_client.py`. Append:

```python
def test_risk_decision_entry_captures_action():
    """RiskDecisionEntry must preserve the action field from Claude's output."""
    from scorched.services.claude_client import RiskDecisionEntry, RiskReviewOutput

    raw = {
        "decisions": [
            {"symbol": "AAPL", "action": "buy", "verdict": "reject", "reason": "too extended"},
            {"symbol": "MSFT", "action": "sell", "verdict": "approve", "reason": "fine"},
        ]
    }
    validated = RiskReviewOutput.model_validate(raw)
    dumped = [d.model_dump() for d in validated.decisions]

    assert dumped[0]["action"] == "buy"
    assert dumped[0]["verdict"] == "reject"
    assert dumped[1]["action"] == "sell"
    assert dumped[1]["verdict"] == "approve"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/tradebot && docker compose exec tradebot pytest tests/test_claude_client.py::test_risk_decision_entry_captures_action -v`

Expected: FAIL (action is not a field on the model, so `dumped[0]["action"]` will raise `KeyError`).

- [ ] **Step 3: Add `action` field to `RiskDecisionEntry`**

Edit `src/scorched/services/claude_client.py` at line 107-120. Replace:

```python
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
```

With:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /home/ubuntu/tradebot && docker compose exec tradebot pytest tests/test_claude_client.py::test_risk_decision_entry_captures_action -v`

Expected: PASS.

- [ ] **Step 5: Add integration test that risk-review filter actually removes rejected buys**

Edit `tests/test_risk_review.py`. Append:

```python
def test_parse_risk_review_preserves_action():
    """Risk review parser must preserve action so recommender can filter rejected buys."""
    from scorched.services.risk_review import parse_risk_review_response

    raw = """{
        "review_summary": "test",
        "decisions": [
            {"symbol": "AAPL", "action": "buy", "verdict": "reject", "reason": "extended"},
            {"symbol": "MSFT", "action": "sell", "verdict": "approve", "reason": "ok"}
        ]
    }"""
    decisions = parse_risk_review_response(raw)
    assert decisions is not None
    assert len(decisions) == 2
    assert decisions[0]["action"] == "buy"
    assert decisions[0]["verdict"] == "reject"
    assert decisions[1]["action"] == "sell"
```

- [ ] **Step 6: Run all risk-review tests and the whole test_claude_client suite**

Run: `docker compose exec tradebot pytest tests/test_risk_review.py tests/test_claude_client.py -v`

Expected: all PASS.

- [ ] **Step 7: Rebuild container and sanity-check**

Run: `cd /home/ubuntu/tradebot && docker compose up -d --build tradebot`
Then: `docker compose exec tradebot python3 -c "from scorched.services.claude_client import RiskDecisionEntry; print(RiskDecisionEntry.model_fields.keys())"`
Expected output includes `action`.

- [ ] **Step 8: Commit**

```bash
cd /home/ubuntu/tradebot
git add src/scorched/services/claude_client.py tests/test_claude_client.py tests/test_risk_review.py
git commit -m "fix: risk committee action field captured so reject verdicts actually filter buys"
```

---

## Task 2: Fix strategy.json path resolution + add missing config blocks

### Gap

Two bugs fuse into one silent failure:

**2a.** `src/scorched/services/strategy.py:158-162`:
```python
def _resolve_path() -> Path:
    path: Path = settings.strategy_file
    if not path.is_absolute():
        path = Path.cwd() / path
    return path
```

When cron runs from `/home/ubuntu` (as the installed crontab does), `Path.cwd()` is `/home/ubuntu` and `strategy.json` resolves to `/home/ubuntu/strategy.json` — which doesn't exist. `load_strategy_json()` returns `DEFAULT_JSON` (line 169-170). Today's `logs/cron.log` literally says `strategy.json not found at /home/ubuntu/strategy.json — using defaults`.

The same bug affects `load_analyst_guidance()` at line 189 (also uses `Path.cwd()`), but that function only runs inside the FastAPI container where `cwd=/app`, so it currently works — fix anyway for robustness.

**2b.** Even with path fixed, `strategy.json` has no `circuit_breaker` or `intraday_monitor` blocks. Phase 1.5 (`cron/tradebot_phase1_5.py:68`) defaults missing `circuit_breaker` to `{"enabled": False}`. Intraday thresholds default to hardcoded values (5% drop, etc.).

Result: Phase 1.5 silently disables all three circuit-breaker gates (stock gap, SPY drop, VIX spike, price drift).

### Files
- Modify: `src/scorched/services/strategy.py` (path resolution)
- Modify: `strategy.json` (add missing blocks)
- Test: `tests/test_strategy_loader.py` (new file)

### Steps

- [ ] **Step 1: Write failing test for strategy.json path resolution**

Create new file `tests/test_strategy_loader.py`:

```python
"""Tests for strategy.json path resolution — must anchor on repo root, not cwd."""
import json
import os
from pathlib import Path

import pytest


def test_resolve_path_anchors_on_repo_root_not_cwd(tmp_path, monkeypatch):
    """Relative strategy_file should resolve against the package root, not Path.cwd()."""
    from scorched.services import strategy as strat

    # Simulate running from a directory that has no strategy.json
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    resolved = strat._resolve_path()
    # Must not resolve to cwd/strategy.json
    assert resolved != elsewhere / "strategy.json", (
        f"Expected repo-anchored path, got cwd-anchored: {resolved}"
    )
    # Must resolve to an existing file (the real strategy.json in the repo)
    assert resolved.exists(), f"Path {resolved} does not exist"


def test_load_strategy_json_from_unrelated_cwd(tmp_path, monkeypatch):
    """load_strategy_json() must return the real config even when cwd is wrong."""
    from scorched.services import strategy as strat

    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    data = strat.load_strategy_json()
    # Real strategy.json has concentration block; DEFAULT_JSON has it too but with
    # a stable sentinel: the real file has max_holdings from disk (likely 5),
    # defaults also have 5. So test the absence of the default-only marker instead:
    # both default and real json should have "concentration" key.
    assert "concentration" in data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/tradebot && docker compose exec tradebot pytest tests/test_strategy_loader.py -v`

Expected: `test_resolve_path_anchors_on_repo_root_not_cwd` FAILS — the path does resolve to `elsewhere/strategy.json`.

- [ ] **Step 3: Anchor `_resolve_path` on the package root**

Edit `src/scorched/services/strategy.py` at line 158-162. Replace:

```python
def _resolve_path() -> Path:
    path: Path = settings.strategy_file
    if not path.is_absolute():
        path = Path.cwd() / path
    return path
```

With:

```python
# Repo root = two levels up from this file's parent (src/scorched/services/strategy.py
# → src/scorched → src → repo_root). Inside Docker this is /app; that's fine —
# strategy.json is mounted there.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _resolve_path() -> Path:
    path: Path = settings.strategy_file
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path
```

Also edit `load_analyst_guidance` at line 187-199. Replace:

```python
def load_analyst_guidance() -> str:
    """Return the analyst_guidance.md content for injection into Claude prompts."""
    path = Path.cwd() / "analyst_guidance.md"
```

With:

```python
def load_analyst_guidance() -> str:
    """Return the analyst_guidance.md content for injection into Claude prompts."""
    path = _REPO_ROOT / "analyst_guidance.md"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec tradebot pytest tests/test_strategy_loader.py -v`

Expected: both tests PASS.

- [ ] **Step 5: Add `circuit_breaker` and `intraday_monitor` blocks to strategy.json**

Edit `/home/ubuntu/tradebot/strategy.json`. Add these two top-level blocks before the closing `}`:

```json
  "circuit_breaker": {
    "enabled": true,
    "stock_gap_down_pct": 2.0,
    "stock_price_drift_pct": 1.5,
    "spy_gap_down_pct": 1.0,
    "vix_absolute_max": 30,
    "vix_spike_pct": 20,
    "stock_gap_up_pct": 5.0
  },
  "intraday_monitor": {
    "enabled": true,
    "position_drop_from_entry_pct": 5.0,
    "position_drop_from_open_pct": 3.0,
    "spy_intraday_drop_pct": 2.0,
    "volume_surge_multiplier": 3.0,
    "hard_stop_pct": 5.0
  },
  "drawdown_gate": {
    "enabled": true,
    "max_drawdown_pct": 8.0
  }
```

(Ensure JSON stays valid — add commas after the `execution` block that precedes them.)

- [ ] **Step 6: Validate JSON + sanity-check load**

Run: `cd /home/ubuntu/tradebot && python3 -c "import json; json.load(open('strategy.json'))"` — expected: no output (valid).

Run: `docker compose up -d --build tradebot`

Run: `docker compose exec tradebot python3 -c "from scorched.services.strategy import load_strategy_json; import json; print(json.dumps(load_strategy_json(), indent=2))" | head -60`

Expected: output includes the new `circuit_breaker` block with `enabled: true`.

- [ ] **Step 7: Dry-run Phase 1.5 against today's recs file (if present) to confirm breaker arms**

Run: `cd /home/ubuntu/tradebot && SETTINGS_PIN="$(grep '^SETTINGS_PIN=' .env | cut -d= -f2)" TRADEBOT_URL=http://127.0.0.1:8000 python3 cron/tradebot_phase1_5.py 2>&1 | tail -20`

Expected: the log line that previously said `Circuit breaker disabled — passing all recommendations through` is GONE (either says "no recs file" or actually runs the breaker).

- [ ] **Step 8: Commit**

```bash
git add src/scorched/services/strategy.py strategy.json tests/test_strategy_loader.py
git commit -m "fix: anchor strategy.json path on repo root; add circuit_breaker/intraday/drawdown config blocks"
```

---

## Task 3: Tighten `.env` permissions and rotate SETTINGS_PIN

### Gap

`.env` is mode `644` — world-readable by any OS user on the VM. Contains `ANTHROPIC_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `TELEGRAM_BOT_TOKEN`, `FINNHUB_API_KEY`, `TWELVEDATA_API_KEY`, `FRED_API_KEY`, `SETTINGS_PIN`. Any compromised process (not running as `ubuntu`) can read it.

`SETTINGS_PIN` is currently 4 digits. Brute-force space: 10,000. On a Tailscale-only deployment this requires an attacker already on the tailnet — but if one is, 10k attempts against `/api/v1/trades/confirm` is trivial and there's no rate limiting.

### Files
- Modify: `/home/ubuntu/tradebot/.env` (chmod + pin rotation)
- Modify: `/home/ubuntu/.tradebot_cron_env` (pin rotation — must match)
- No code changes.

### Steps

- [ ] **Step 1: Verify current .env permissions**

Run: `ls -la /home/ubuntu/tradebot/.env`
Expected: shows `-rw-r--r--` (the bug we're fixing).

- [ ] **Step 2: Generate a new PIN (16 random chars, safe for headers)**

Run: `python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20)))"`

Capture the output. Call this `$NEW_PIN` for the next steps. Do NOT paste the value into any log or commit message.

- [ ] **Step 3: Update SETTINGS_PIN in /home/ubuntu/tradebot/.env**

Edit `.env` and replace the `SETTINGS_PIN=...` line with `SETTINGS_PIN=$NEW_PIN`.

(Use a text editor or `sed -i "s/^SETTINGS_PIN=.*/SETTINGS_PIN=$NEW_PIN/" /home/ubuntu/tradebot/.env` — then clear shell history afterward.)

- [ ] **Step 4: Update /home/ubuntu/.tradebot_cron_env with the same PIN**

Edit `/home/ubuntu/.tradebot_cron_env` — replace the `SETTINGS_PIN=...` line with the same new value.

- [ ] **Step 5: Tighten permissions on both files**

Run:
```bash
chmod 600 /home/ubuntu/tradebot/.env
chmod 600 /home/ubuntu/.tradebot_cron_env
ls -la /home/ubuntu/tradebot/.env /home/ubuntu/.tradebot_cron_env
```

Expected: both show `-rw-------`.

- [ ] **Step 6: Restart the container so it picks up the new PIN**

Run: `cd /home/ubuntu/tradebot && docker compose up -d tradebot`

Wait ~10 seconds, then:
Run: `docker compose exec tradebot python3 -c "from scorched.config import settings; print('pin_len:', len(settings.settings_pin))"`
Expected: `pin_len: 20`.

- [ ] **Step 7: Sanity-check PIN enforcement**

Run (expecting 401):
```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/api/v1/strategy -H 'Content-Type: application/json' -d '{"_pin":"0000"}'
```
Expected: `403` or `401` (whatever the endpoint returns for wrong PIN).

- [ ] **Step 8: Sanity-check cron can still authenticate**

Run: `. /home/ubuntu/.tradebot_cron_env && cd /home/ubuntu/tradebot && python3 -c "from cron.common import http_get; print(http_get('/api/v1/system/health')[:200])"`
Expected: JSON health payload (not a 401).

- [ ] **Step 9: No git commit**

This task does not touch tracked files. Don't commit `.env` (it's gitignored anyway). Verify with: `git status` — should show no new staged changes from this task.

---

## Task 4: Gate `/api/v1/onboarding/*` behind PIN

### Gap

`src/scorched/api/onboarding.py` exposes three endpoints:
- `POST /api/v1/onboarding/validate-key` (line 177) — outbound probe against provider APIs
- `POST /api/v1/onboarding/save` (line 270) — rewrites `.env`
- `GET /api/v1/onboarding/status` (line 315) — reveals broker mode + starting capital

Only `save` has an inline PIN check, and it only fires when `settings.settings_pin` is already set. First-caller-on-fresh-install wins — they can set `BROKER_MODE=alpaca_live` with attacker-controlled keys. `validate-key` and `status` are fully ungated.

### Files
- Modify: `src/scorched/api/onboarding.py` — add `dependencies=[Depends(require_owner_pin)]` to the three routes.
- Test: `tests/test_onboarding_auth.py` (new file)

### Steps

- [ ] **Step 1: Read the current decorators to capture exact form**

Run: `grep -n '@router\.' /home/ubuntu/tradebot/src/scorched/api/onboarding.py`

Expected output shows three `@router.post(...)` or `@router.get(...)` lines near lines 177, 270, 315.

- [ ] **Step 2: Write failing test**

Create `tests/test_onboarding_auth.py`:

```python
"""Onboarding endpoints must require the X-Owner-Pin header when a PIN is configured."""
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_onboarding_status_requires_pin(monkeypatch):
    """GET /api/v1/onboarding/status must 403 without X-Owner-Pin when PIN configured."""
    monkeypatch.setenv("SETTINGS_PIN", "test-pin-long-enough-1234")
    from scorched import config as cfg
    cfg.settings = cfg.Settings()  # reload with new env
    from scorched.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/api/v1/onboarding/status")
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_onboarding_validate_key_requires_pin(monkeypatch):
    monkeypatch.setenv("SETTINGS_PIN", "test-pin-long-enough-1234")
    from scorched import config as cfg
    cfg.settings = cfg.Settings()
    from scorched.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post("/api/v1/onboarding/validate-key", json={"service": "polygon", "key": "x"})
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_onboarding_save_requires_pin(monkeypatch):
    monkeypatch.setenv("SETTINGS_PIN", "test-pin-long-enough-1234")
    from scorched import config as cfg
    cfg.settings = cfg.Settings()
    from scorched.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post("/api/v1/onboarding/save", json={})
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `docker compose exec tradebot pytest tests/test_onboarding_auth.py -v`

Expected: all three FAIL — likely returning 200 or 422 (validation error), not 403.

- [ ] **Step 4: Add `Depends(require_owner_pin)` to all three onboarding routes**

Open `src/scorched/api/onboarding.py`. At the top of the file, ensure these imports exist (add if missing):

```python
from fastapi import Depends
from .deps import require_owner_pin
```

For each of the three routes (`validate-key`, `save`, `status`), modify the decorator. Example pattern — a line that currently reads:

```python
@router.post("/validate-key")
```

becomes:

```python
@router.post("/validate-key", dependencies=[Depends(require_owner_pin)])
```

Apply the same change to the `save` and `status` routes.

Also, inside `save()` you can now remove the inline PIN check at around line 281-284 (it's now redundant with the dependency). Double-check the exact line range by reading the file; remove only the `if pin != settings.settings_pin` block, leaving `_write_env` and the rest intact.

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker compose exec tradebot pytest tests/test_onboarding_auth.py -v`

Expected: all PASS.

- [ ] **Step 6: Manual smoke test against the running container**

Run (expect 403):
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/v1/onboarding/status
```
Expected: `403`.

Run (expect 200):
```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Owner-Pin: $(grep '^SETTINGS_PIN=' /home/ubuntu/tradebot/.env | cut -d= -f2)" http://127.0.0.1:8000/api/v1/onboarding/status
```
Expected: `200`.

- [ ] **Step 7: Commit**

```bash
git add src/scorched/api/onboarding.py tests/test_onboarding_auth.py
git commit -m "fix: require X-Owner-Pin for /api/v1/onboarding/* endpoints"
```

---

## Task 5: Harden PID locks to detect hung processes

### Gap

`cron/common.py:12-26`:

```python
def acquire_lock(name):
    lock_path = f"/tmp/tradebot_{name}.lock"
    if os.path.exists(lock_path):
        try:
            with open(lock_path) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)  # signal 0 = check existence
            print(f"Another {name} instance running (PID {old_pid}), exiting")
            sys.exit(0)
        ...
```

`os.kill(pid, 0)` returns success for a running-but-hung process. There's no lock-age check, no `fcntl.flock`. Today's `cron.log` shows this caused a ~3-hour dead-zone when intraday PID 18760 hung at ~12:15 and didn't die until 15:05 — every 5-minute tick in that window silently exited.

### Files
- Modify: `cron/common.py` (lock acquire/release)
- Test: `tests/test_cron_lock.py` (new file)

### Steps

- [ ] **Step 1: Write failing tests for lock-age reclaim**

Create `tests/test_cron_lock.py`:

```python
"""PID lock must detect stale locks older than MAX_LOCK_AGE_S and reclaim them."""
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest


def test_lock_reclaim_when_older_than_max_age(tmp_path, monkeypatch):
    from cron import common

    # Redirect lock file into tmp_path so we don't collide with a real lock
    lock = tmp_path / "tradebot_test.lock"
    monkeypatch.setattr(common, "_lock_path_for", lambda name: str(lock))

    # Write a stale lock: a process that looks alive (use parent PID) but with
    # a very old mtime
    lock.write_text(str(os.getppid()))
    old_time = time.time() - (common.MAX_LOCK_AGE_S + 60)
    os.utime(lock, (old_time, old_time))

    # acquire_lock should reclaim (not sys.exit)
    common.acquire_lock("test")

    # Lock should now contain our current PID
    assert int(lock.read_text().strip()) == os.getpid()
    common.release_lock("test")


def test_lock_blocks_when_recent_and_alive(tmp_path, monkeypatch):
    from cron import common

    lock = tmp_path / "tradebot_test.lock"
    monkeypatch.setattr(common, "_lock_path_for", lambda name: str(lock))

    lock.write_text(str(os.getppid()))  # alive PID
    # Fresh mtime
    os.utime(lock, None)

    with pytest.raises(SystemExit):
        common.acquire_lock("test")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec tradebot pytest tests/test_cron_lock.py -v`

Expected: FAIL — `_lock_path_for` and `MAX_LOCK_AGE_S` don't exist yet.

- [ ] **Step 3: Refactor `acquire_lock`/`release_lock` to add age check**

Edit `cron/common.py`. Replace the top section (imports + lock functions, lines 1-35) with:

```python
"""Shared utilities for cron phase scripts."""
import datetime
import json
import os
import pathlib
import sys
import time
import urllib.request
import urllib.error

import pytz


MAX_LOCK_AGE_S = 10 * 60  # reclaim a lock older than this (process assumed hung)


def _lock_path_for(name: str) -> str:
    return f"/tmp/tradebot_{name}.lock"


def acquire_lock(name):
    """Acquire a PID lock. Exits 0 if another instance is actively running.

    A lock file older than MAX_LOCK_AGE_S is treated as stale and reclaimed —
    this prevents a hung process from blocking cron runs forever.
    """
    lock_path = _lock_path_for(name)
    if os.path.exists(lock_path):
        age = time.time() - os.path.getmtime(lock_path)
        try:
            with open(lock_path) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)  # raises ProcessLookupError if dead
            # Process exists. If the lock is old, reclaim it (hung process).
            if age > MAX_LOCK_AGE_S:
                _reclaim_stale_lock(name, old_pid, age)
            else:
                print(f"Another {name} instance running (PID {old_pid}, age {age:.0f}s), exiting")
                sys.exit(0)
        except (ProcessLookupError, ValueError, FileNotFoundError):
            pass  # dead or unreadable lock — reclaim below
    with open(lock_path, "w") as f:
        f.write(str(os.getpid()))


def _reclaim_stale_lock(name: str, old_pid: int, age_s: float) -> None:
    msg = (
        f"TRADEBOT // Stale lock reclaimed for {name}: "
        f"PID {old_pid} alive but lock age {age_s/60:.1f} min exceeds "
        f"MAX_LOCK_AGE_S={MAX_LOCK_AGE_S/60:.0f} min — evicting"
    )
    print(msg)
    try:
        send_telegram(msg)
    except Exception as e:
        print(f"(Telegram notify failed: {e})")


def release_lock(name):
    """Release the PID lock file."""
    try:
        os.remove(_lock_path_for(name))
    except FileNotFoundError:
        pass
```

Note: `send_telegram` is defined later in the same file, so the reference inside `_reclaim_stale_lock` works at call time (not at import time). If you prefer strict ordering, move `_reclaim_stale_lock` below `send_telegram`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec tradebot pytest tests/test_cron_lock.py -v`

Expected: both PASS.

- [ ] **Step 5: Smoke-test against a real cron script (dry-run)**

Run: `cd /home/ubuntu/tradebot && python3 -c "from cron.common import acquire_lock, release_lock; acquire_lock('smoke'); release_lock('smoke'); print('ok')"`
Expected: `ok` with no errors.

- [ ] **Step 6: Commit**

```bash
git add cron/common.py tests/test_cron_lock.py
git commit -m "fix: reclaim stale PID locks older than 10 min so hung cron jobs don't block subsequent runs"
```

---

## Task 6: Render Twelvedata RSI and economic calendar in Claude context

### Gap

Phase 0 fetches two data streams that never reach Claude:

- **Twelvedata RSI** (`src/scorched/api/prefetch.py:182`): ~70 symbols × ~0.5s = ~35s/day. Cached as `twelvedata_rsi`. `build_research_context()` in `src/scorched/services/research.py` does not accept or render it.
- **Economic calendar** (`src/scorched/api/prefetch.py:190`): Fetches upcoming CPI/FOMC/Jobs releases. Cached as `economic_calendar_context`. Never rendered.

Claude sees RSI only for the <20 Alpha Vantage screener picks, and has no awareness of same-day macro releases.

### Files
- Modify: `src/scorched/services/research.py` (`build_research_context` signature + rendering)
- Modify: `src/scorched/services/recommender.py` (pass cached values into `build_research_context`)
- Modify: `src/scorched/api/prefetch.py` (ensure cache keys are preserved — likely no change needed)
- Test: `tests/test_research_context.py` (new file)

### Steps

- [ ] **Step 1: Locate the current signature of `build_research_context`**

Run: `grep -n 'def build_research_context' /home/ubuntu/tradebot/src/scorched/services/research.py`

Expected: one hit. Note the exact line number and existing keyword args.

- [ ] **Step 2: Write failing test for twelvedata RSI rendering**

Create `tests/test_research_context.py`:

```python
"""build_research_context must surface twelvedata_rsi and economic calendar to Claude."""


def test_twelvedata_rsi_appears_in_context():
    from scorched.services.research import build_research_context

    price_data = {"AAPL": {"current_price": 200.0, "week_change_pct": 4.2}}
    twelvedata_rsi = {"AAPL": 62.3}

    ctx = build_research_context(
        price_data=price_data,
        av_technicals={},
        macro_context="",
        news={},
        polygon_news={},
        options_data={},
        insider_activity={},
        earnings_surprise={},
        finnhub_consensus={},
        sector_returns={},
        premarket_data={},
        twelvedata_rsi=twelvedata_rsi,
        economic_calendar_context="",
        held_symbols=set(),
    )
    assert "RSI" in ctx and "62.3" in ctx, f"RSI not rendered. Full context:\n{ctx[:2000]}"


def test_economic_calendar_appears_in_context():
    from scorched.services.research import build_research_context

    price_data = {"AAPL": {"current_price": 200.0, "week_change_pct": 4.2}}
    econ = "UPCOMING RELEASES:\n- 2026-04-18: CPI (high impact)"

    ctx = build_research_context(
        price_data=price_data,
        av_technicals={},
        macro_context="",
        news={},
        polygon_news={},
        options_data={},
        insider_activity={},
        earnings_surprise={},
        finnhub_consensus={},
        sector_returns={},
        premarket_data={},
        twelvedata_rsi={},
        economic_calendar_context=econ,
        held_symbols=set(),
    )
    assert "CPI" in ctx, f"Economic calendar not rendered. Context:\n{ctx[:2000]}"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `docker compose exec tradebot pytest tests/test_research_context.py -v`

Expected: FAIL — `build_research_context` doesn't accept `twelvedata_rsi` or `economic_calendar_context` yet.

- [ ] **Step 4: Extend `build_research_context` signature + rendering**

Edit `src/scorched/services/research.py`. Find `def build_research_context(...)` and add two new keyword arguments with default `None`:

```python
def build_research_context(
    # ... existing args ...
    twelvedata_rsi: dict | None = None,
    economic_calendar_context: str | None = None,
    # ... rest ...
):
```

Inside the function, render Twelvedata RSI per-symbol wherever AV RSI is currently rendered. Typical pattern (find the line that emits `f"  RSI(14): {rsi:.1f}"` and extend it):

```python
rsi_av = (av_technicals or {}).get(sym, {}).get("rsi")
rsi_td = (twelvedata_rsi or {}).get(sym)
rsi = rsi_av if rsi_av is not None else rsi_td
if rsi is not None:
    lines.append(f"  RSI(14): {rsi:.1f}")
```

Near the top of the market-context block (find the existing `=== MARKET CONTEXT ===` or similar), prepend the economic calendar if present:

```python
if economic_calendar_context:
    lines.append("=== UPCOMING ECONOMIC RELEASES ===")
    lines.append(economic_calendar_context)
    lines.append("")
```

(Exact placement depends on the current layout — read around the relevant section to keep ordering sensible. The test only asserts presence, not position.)

- [ ] **Step 5: Update `recommender.py` to pass the new kwargs**

Find the call to `build_research_context(...)` in `src/scorched/services/recommender.py` (likely one call site in `generate_recommendations`). Add two kwargs from the cached/inline payload:

```python
context = build_research_context(
    # ... existing kwargs ...
    twelvedata_rsi=research_data.get("twelvedata_rsi", {}),
    economic_calendar_context=research_data.get("economic_calendar_context", ""),
)
```

If the cache dict uses different key names, `grep -n 'twelvedata_rsi\|economic_calendar' src/scorched/api/prefetch.py` to confirm.

- [ ] **Step 6: Run tests to verify they pass**

Run: `docker compose exec tradebot pytest tests/test_research_context.py -v`

Expected: both PASS.

- [ ] **Step 7: Rebuild + spot-check against today's cache**

Run: `cd /home/ubuntu/tradebot && docker compose up -d --build tradebot`

Run:
```bash
docker compose exec tradebot python3 -c "
import json
from scorched.services.research import build_research_context
d = json.load(open('logs/tradebot_research_cache_2026-04-17.json'))
# Map the cache back to kwargs. Field names must match what's in the cache.
ctx = build_research_context(
    price_data=d.get('price_data', {}),
    av_technicals=d.get('av_technicals', {}),
    macro_context=d.get('macro_context', ''),
    news=d.get('news', {}),
    polygon_news=d.get('polygon_news', {}),
    options_data=d.get('options_data', {}),
    insider_activity=d.get('insider_activity', {}),
    earnings_surprise=d.get('earnings_surprise', {}),
    finnhub_consensus=d.get('finnhub_consensus', {}),
    sector_returns=d.get('sector_returns', {}),
    premarket_data=d.get('premarket_data', {}),
    twelvedata_rsi=d.get('twelvedata_rsi', {}),
    economic_calendar_context=d.get('economic_calendar_context', ''),
    held_symbols=set(),
)
print('RSI mentions:', ctx.count('RSI'))
print('First 2000 chars:', ctx[:2000])
"
```

Expected: `RSI mentions:` > 10 (previously was limited to <20 AV picks).

- [ ] **Step 8: Commit**

```bash
git add src/scorched/services/research.py src/scorched/services/recommender.py tests/test_research_context.py
git commit -m "feat: render twelvedata RSI and economic calendar in Claude context"
```

---

## Task 7: Finish Alpaca news wiring in Phase 0

### Gap

`src/scorched/api/prefetch.py:122`:

```python
polygon_news = {}  # Polygon removed, replaced by Alpaca news (TODO: wire up)
```

(Exact wording may differ; the line hardcodes an empty dict.)

The function `fetch_polygon_news` — despite its name — now routes to Alpaca news (see `src/scorched/services/research.py` where it's implemented). So the migration from Polygon to Alpaca news was partially done: the function was rewritten but never re-plugged into Phase 0. Today the happy path (cache hit) ships Claude zero news article bodies; only yfinance headlines survive.

Secondary hygiene: `fetch_polygon_news` still takes a `polygon_api_key` parameter it no longer uses, and `settings.polygon_api_key` still exists. Rename & remove as part of this task.

### Files
- Modify: `src/scorched/api/prefetch.py` (wire Alpaca news into the gather)
- Modify: `src/scorched/services/research.py` (rename + drop unused kwarg)
- Modify: `src/scorched/services/recommender.py` (update caller if it uses the removed kwarg)
- Test: manual log inspection after rebuild

### Steps

- [ ] **Step 1: Locate the stubbed line in prefetch.py**

Run: `grep -n 'polygon_news' /home/ubuntu/tradebot/src/scorched/api/prefetch.py`

Record the line numbers. Expect to see a `polygon_news = {}` assignment plus a reference inside the cache dict.

- [ ] **Step 2: Locate `fetch_polygon_news` signature**

Run: `grep -n 'async def fetch_polygon_news\|def _fetch_polygon_news_sync' /home/ubuntu/tradebot/src/scorched/services/research.py`

Read the function body (~20 lines) to confirm its current dependencies — specifically whether it actually uses `polygon_api_key` or just Alpaca. Expected: uses Alpaca; `api_key` is ignored.

- [ ] **Step 3: Rename `fetch_polygon_news` → `fetch_detailed_news` and drop the unused kwarg**

Edit `src/scorched/services/research.py`:

- Rename `async def fetch_polygon_news(...)` → `async def fetch_detailed_news(...)`.
- Rename the sync helper similarly.
- Remove the `api_key` parameter from both signatures and from all internal references.
- Leave a thin alias for backward compatibility during the migration:

```python
# Backwards-compat alias; remove after all callers are migrated
fetch_polygon_news = fetch_detailed_news
```

- [ ] **Step 4: Wire the call into prefetch.py's parallel gather**

Edit `src/scorched/api/prefetch.py`. Replace the stub block:

```python
polygon_news = {}
```

with an `asyncio.gather` participant. Use the same `_timed_fetch(...)` helper pattern as the other fetches:

```python
detailed_news_task = _timed_fetch(
    "alpaca_news",
    fetch_detailed_news(research_symbols, tracker=tracker),
)
```

Add `detailed_news_task` to the `asyncio.gather(...)` call a few lines below. In the cache dict, rename the key from `"polygon_news"` to `"detailed_news"` (update any consumer in `recommender.py` accordingly).

If there's a consumer that still expects the `polygon_news` key, keep both keys populated during the migration window:

```python
cache_payload["polygon_news"] = detailed_news  # legacy key
cache_payload["detailed_news"] = detailed_news
```

- [ ] **Step 5: Update `recommender.py` caller(s)**

Run: `grep -n 'polygon_news\|fetch_polygon_news' /home/ubuntu/tradebot/src/scorched/services/recommender.py`

For each hit:
- Inline-fetch fallback call: rename to `fetch_detailed_news(research_symbols, tracker=tracker)` — drop the `api_key` arg.
- `build_research_context(...)` call: you may keep `polygon_news=` as the kwarg name *only if* `build_research_context` still uses it. Otherwise rename consistently.

- [ ] **Step 6: Remove `polygon_api_key` references that are now dead**

Run: `grep -rn 'polygon_api_key' /home/ubuntu/tradebot/src/`

Remove the field from `config.py`, any dead reads in `onboarding.py`, and `DATA_SOURCES.md`. (Leave `.env.example` alone for now — it'll be cleaned in the docs sweep.)

- [ ] **Step 7: Rebuild and trigger a fresh Phase 0 run**

Run: `cd /home/ubuntu/tradebot && docker compose up -d --build tradebot`

Wait 15s, then run:
```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/research/prefetch \
  -H "X-Owner-Pin: $(grep '^SETTINGS_PIN=' .env | cut -d= -f2)" \
  -H 'Content-Type: application/json' -d '{}' | jq '.timings.alpaca_news, .cache_file'
```

Expected: a numeric timing (e.g. `12.3`), not `null` — and a cache file path.

- [ ] **Step 8: Verify news made it into the cache**

Run:
```bash
docker compose exec tradebot python3 -c "
import json
d = json.load(open('logs/tradebot_research_cache_2026-04-17.json'))
key = 'detailed_news' if 'detailed_news' in d else 'polygon_news'
total_articles = sum(len(v) for v in d[key].values() if isinstance(v, list))
print(f'symbols with news: {sum(1 for v in d[key].values() if v)}')
print(f'total articles: {total_articles}')
"
```

Expected: `symbols with news: > 10`, `total articles: > 50` on a typical news day.

- [ ] **Step 9: Run the full pytest suite for regressions**

Run: `docker compose exec tradebot pytest -x -q`
Expected: all green.

- [ ] **Step 10: Commit**

```bash
git add src/scorched/api/prefetch.py src/scorched/services/research.py src/scorched/services/recommender.py src/scorched/config.py src/scorched/api/onboarding.py
git commit -m "feat: wire Alpaca detailed news into Phase 0; rename fetch_polygon_news; drop dead polygon_api_key"
```

---

## Task 8: Wire trailing stops into intraday monitor

### Gap

- `src/scorched/trailing_stops.py:13` (`compute_trailing_stop`) — tested, never called.
- `src/scorched/intraday.py:13` (`check_trailing_stop_breach`) — tested, never called.
- `src/scorched/services/portfolio.py:194` — `apply_buy` hardcodes `trailing_stop_price = execution_price * 0.95` and `high_water_mark = execution_price`. Neither is ever updated after the initial buy.
- `cron/intraday_monitor.py` does not call trailing-stop logic.

Net effect: trailing-stop advertising in CLAUDE.md and `advisor.md` is marketing; the code enforces a flat -5% hard stop.

### Files
- Modify: `src/scorched/services/portfolio.py` (use `compute_trailing_stop` at buy + add an update helper)
- Modify: `src/scorched/intraday.py` (call `check_trailing_stop_breach` inside `check_intraday_triggers`)
- Modify: `cron/intraday_monitor.py` (on each tick, update HWM + stop for each held position)
- Test: extend `tests/test_trailing_stops.py` and `tests/test_intraday.py`

### Steps

- [ ] **Step 1: Read the current trailing-stop implementation**

Run: `cat /home/ubuntu/tradebot/src/scorched/trailing_stops.py`

Confirm the signature of `compute_trailing_stop(entry_price, current_price, atr, high_water_mark)` — or whatever it actually takes. Adjust the test + wiring below to match the real signature.

- [ ] **Step 2: Read existing trailing stop tests**

Run: `cat /home/ubuntu/tradebot/tests/test_trailing_stops.py`

Understand the conventions (floor at -5%, ATR multiple of 2).

- [ ] **Step 3: Write failing test for intraday trailing-stop trigger**

Edit `tests/test_intraday.py`. Append:

```python
def test_check_intraday_triggers_includes_trailing_stop():
    """A position whose current price crosses below its trailing stop must trigger."""
    from scorched.intraday import check_intraday_triggers

    position = {
        "symbol": "AAPL",
        "shares": 10,
        "entry_price": 100.0,
        "current_price": 108.0,  # below trailing stop
        "day_open_price": 110.0,
        "avg_volume": 1_000_000,
        "current_volume": 1_000_000,
        "high_water_mark": 115.0,
        "trailing_stop_price": 110.0,  # breached
    }
    config = {
        "position_drop_from_entry_pct": 20,  # so entry-drop doesn't fire
        "position_drop_from_open_pct": 20,
        "volume_surge_multiplier": 10,
    }
    triggers = check_intraday_triggers(position, config)
    kinds = [t["kind"] for t in triggers]
    assert "trailing_stop" in kinds, f"trailing_stop not in triggers: {kinds}"
```

- [ ] **Step 4: Run test to verify it fails**

Run: `docker compose exec tradebot pytest tests/test_intraday.py::test_check_intraday_triggers_includes_trailing_stop -v`

Expected: FAIL.

- [ ] **Step 5: Add trailing-stop check to `check_intraday_triggers`**

Edit `src/scorched/intraday.py`. Inside `check_intraday_triggers(position, config)`, after the existing per-position trigger checks and before returning, add:

```python
from .trailing_stops import check_trailing_stop_breach

stop = position.get("trailing_stop_price")
price = position.get("current_price")
if stop is not None and price is not None and check_trailing_stop_breach(price, stop):
    triggers.append({
        "kind": "trailing_stop",
        "symbol": position["symbol"],
        "current_price": price,
        "trailing_stop_price": stop,
        "high_water_mark": position.get("high_water_mark"),
        "reason": f"Price {price:.2f} below trailing stop {stop:.2f}",
    })
```

(Exact shape of the trigger dict — `kind`, `reason`, etc. — should match what the existing triggers use. Read one of the existing ones in the same file first.)

- [ ] **Step 6: Run test to verify it passes**

Run: `docker compose exec tradebot pytest tests/test_intraday.py::test_check_intraday_triggers_includes_trailing_stop -v`

Expected: PASS.

- [ ] **Step 7: Write test for HWM/stop ratchet update**

Edit `tests/test_trailing_stops.py`. Append:

```python
def test_update_trailing_stop_ratchets_hwm_up():
    """update_trailing_stop must raise HWM and tighten stop when price makes new high."""
    from scorched.trailing_stops import update_trailing_stop

    state = {"high_water_mark": 100.0, "trailing_stop_price": 95.0}
    atr = 2.0
    new_state = update_trailing_stop(state, current_price=105.0, atr=atr, entry_price=90.0)

    assert new_state["high_water_mark"] == 105.0
    assert new_state["trailing_stop_price"] > 95.0  # tightened
    # Must respect floor: stop >= entry * 0.95
    assert new_state["trailing_stop_price"] >= 90.0 * 0.95


def test_update_trailing_stop_does_not_lower_stop_on_pullback():
    from scorched.trailing_stops import update_trailing_stop

    state = {"high_water_mark": 105.0, "trailing_stop_price": 101.0}
    new_state = update_trailing_stop(state, current_price=103.0, atr=2.0, entry_price=90.0)

    # HWM unchanged, stop unchanged (ratchet, not floor)
    assert new_state["high_water_mark"] == 105.0
    assert new_state["trailing_stop_price"] == 101.0
```

- [ ] **Step 8: Run test to verify it fails**

Run: `docker compose exec tradebot pytest tests/test_trailing_stops.py::test_update_trailing_stop_ratchets_hwm_up -v`

Expected: FAIL — `update_trailing_stop` doesn't exist yet.

- [ ] **Step 9: Implement `update_trailing_stop`**

Edit `src/scorched/trailing_stops.py`. Append:

```python
def update_trailing_stop(
    state: dict,
    current_price: float,
    atr: float,
    entry_price: float,
) -> dict:
    """Ratchet high_water_mark + trailing stop on a new high.

    Stop is max(prior_stop, hwm - 2*atr), floored at entry * 0.95.
    Returns a new dict (does not mutate input).
    """
    hwm = max(state.get("high_water_mark") or current_price, current_price)
    atr_based_stop = hwm - 2 * atr
    floor = entry_price * 0.95
    new_stop = max(state.get("trailing_stop_price") or floor, atr_based_stop, floor)
    return {"high_water_mark": hwm, "trailing_stop_price": new_stop}
```

- [ ] **Step 10: Run trailing-stop tests**

Run: `docker compose exec tradebot pytest tests/test_trailing_stops.py -v`

Expected: all PASS.

- [ ] **Step 11: Use `compute_trailing_stop` at buy time in portfolio**

Edit `src/scorched/services/portfolio.py`. Find `apply_buy` near line 194. Replace the hardcoded stop setup:

```python
position.trailing_stop_price = execution_price * Decimal("0.95")
position.high_water_mark = execution_price
```

with (assuming the ATR is available in the call — if not, pass it through or fetch it):

```python
from ..trailing_stops import compute_trailing_stop
initial = compute_trailing_stop(
    entry_price=float(execution_price), current_price=float(execution_price),
    atr=float(atr or 0.0),
)
position.high_water_mark = Decimal(str(initial["high_water_mark"]))
position.trailing_stop_price = Decimal(str(initial["trailing_stop_price"]))
```

If `compute_trailing_stop` has a different signature, adapt accordingly. If ATR isn't available at buy time, fall back to the -5% floor and update on first intraday tick.

- [ ] **Step 12: Wire ratchet into intraday_monitor**

Edit `cron/intraday_monitor.py`. After fetching the latest snapshot for each held position (before computing triggers), update the persisted HWM + stop:

```python
from scorched.trailing_stops import update_trailing_stop
# ...
for pos in positions:
    sym = pos["symbol"]
    current_price = snapshots.get(sym, {}).get("current_price")
    atr = pos.get("atr") or 0.0
    if current_price and atr > 0:
        new_state = update_trailing_stop(
            {"high_water_mark": pos.get("high_water_mark"), "trailing_stop_price": pos.get("trailing_stop_price")},
            current_price=float(current_price),
            atr=float(atr),
            entry_price=float(pos["entry_price"]),
        )
        # Persist via existing endpoint or DB write helper.
        # If an update endpoint exists, POST to it. Otherwise add a helper on the API.
        pos["high_water_mark"] = new_state["high_water_mark"]
        pos["trailing_stop_price"] = new_state["trailing_stop_price"]
```

**Persistence note:** If there's no existing "update position" API endpoint, add a minimal one: `POST /api/v1/portfolio/positions/{symbol}/trailing-stop` that takes `{hwm, stop}`. Gate it with `require_owner_pin`. Don't add a full CRUD surface — just this one field pair. (If adding that endpoint grows beyond ~20 lines, break it into its own follow-up task.)

- [ ] **Step 13: Run the full test suite**

Run: `docker compose exec tradebot pytest -x -q`
Expected: all green.

- [ ] **Step 14: Rebuild + dry-run intraday_monitor against a non-empty portfolio**

Run: `cd /home/ubuntu/tradebot && docker compose up -d --build tradebot`

Run: `. /home/ubuntu/.tradebot_cron_env && python3 cron/intraday_monitor.py 2>&1 | tail -30`

Expected: log shows "Updated trailing stops for N positions" or similar; no trigger cascade on quiet day.

- [ ] **Step 15: Commit**

```bash
git add src/scorched/trailing_stops.py src/scorched/intraday.py src/scorched/services/portfolio.py cron/intraday_monitor.py tests/test_trailing_stops.py tests/test_intraday.py
git commit -m "feat: wire ATR-based trailing stops into intraday monitor"
```

---

## Task 9: Enforce `max_sector_pct` in code

### Gap

`strategy.json.concentration.max_sector_pct` (40%) is only rendered into the prose shown to Claude — no programmatic enforcement anywhere. `grep -rn "max_sector_pct" src/` returns only config-load paths, no gate. Claude also has no sector metadata per ticker to self-enforce.

### Files
- Modify: `src/scorched/services/research.py` (expand `_SECTOR_ETF_MAP` coverage)
- Modify: `src/scorched/services/recommender.py` (new sector-exposure gate next to position-size gate)
- Test: `tests/test_sector_gate.py` (new file)

### Steps

- [ ] **Step 1: Locate `_SECTOR_ETF_MAP` and count coverage**

Run:
```bash
grep -n '_SECTOR_ETF_MAP\|_SP500_POOL' /home/ubuntu/tradebot/src/scorched/services/research.py | head -10
```

Then:
```bash
docker compose exec tradebot python3 -c "
from scorched.services.research import _SECTOR_ETF_MAP, _SP500_POOL
covered = sum(1 for s in _SP500_POOL if s in _SECTOR_ETF_MAP)
print(f'{covered}/{len(_SP500_POOL)} covered')
"
```

Expected: ~222/434 (per audit).

- [ ] **Step 2: Write failing test for sector exposure gate**

Create `tests/test_sector_gate.py`:

```python
"""Sector concentration gate — reject a buy that would push a sector over max_sector_pct."""
from decimal import Decimal


def test_sector_gate_rejects_when_exceeds_limit():
    from scorched.services.recommender import check_sector_exposure

    held_positions = [
        {"symbol": "AAPL", "sector": "Technology", "market_value": Decimal("30000")},
        {"symbol": "MSFT", "sector": "Technology", "market_value": Decimal("10000")},
    ]
    total_value = Decimal("100000")
    max_sector_pct = 40.0

    # Proposed: buy $5k of NVDA (Tech). Current Tech = 40k / 100k = 40%. +5k = 45% > 40%.
    ok = check_sector_exposure(
        proposed_symbol="NVDA",
        proposed_sector="Technology",
        proposed_dollars=Decimal("5000"),
        held_positions=held_positions,
        total_value=total_value,
        max_sector_pct=max_sector_pct,
    )
    assert ok is False


def test_sector_gate_allows_when_under_limit():
    from scorched.services.recommender import check_sector_exposure

    held_positions = [
        {"symbol": "JPM", "sector": "Financials", "market_value": Decimal("10000")},
    ]
    ok = check_sector_exposure(
        proposed_symbol="NVDA",
        proposed_sector="Technology",
        proposed_dollars=Decimal("15000"),
        held_positions=held_positions,
        total_value=Decimal("100000"),
        max_sector_pct=40.0,
    )
    assert ok is True


def test_sector_gate_permits_unknown_sector_with_warning():
    """Unknown sector should not silently block — log + allow."""
    from scorched.services.recommender import check_sector_exposure

    ok = check_sector_exposure(
        proposed_symbol="ZZZZ",
        proposed_sector=None,
        proposed_dollars=Decimal("5000"),
        held_positions=[],
        total_value=Decimal("100000"),
        max_sector_pct=40.0,
    )
    assert ok is True
```

- [ ] **Step 3: Run test to verify it fails**

Run: `docker compose exec tradebot pytest tests/test_sector_gate.py -v`

Expected: FAIL — `check_sector_exposure` doesn't exist.

- [ ] **Step 4: Implement `check_sector_exposure`**

Edit `src/scorched/services/recommender.py`. Add near the other gate helpers (above `generate_recommendations`):

```python
def check_sector_exposure(
    proposed_symbol: str,
    proposed_sector: str | None,
    proposed_dollars: Decimal,
    held_positions: list[dict],
    total_value: Decimal,
    max_sector_pct: float,
) -> bool:
    """Return True if the proposed buy keeps sector exposure <= max_sector_pct.

    Unknown sector (None) returns True with a warning — we don't block on
    missing metadata.
    """
    if proposed_sector is None:
        logger.warning("Sector gate: %s has no sector metadata — allowing", proposed_symbol)
        return True
    if total_value <= 0:
        return True

    current_sector_value = sum(
        (p.get("market_value") or Decimal("0"))
        for p in held_positions
        if (p.get("sector") or "").lower() == proposed_sector.lower()
    )
    post_buy_value = current_sector_value + proposed_dollars
    post_buy_pct = float(post_buy_value) / float(total_value) * 100

    if post_buy_pct > max_sector_pct:
        logger.info(
            "Sector gate REJECT %s: %s exposure would be %.1f%% > %.1f%% cap",
            proposed_symbol, proposed_sector, post_buy_pct, max_sector_pct,
        )
        return False
    return True
```

- [ ] **Step 5: Expand `_SECTOR_ETF_MAP` coverage**

Edit `src/scorched/services/research.py`. For any symbol in `_SP500_POOL` not present in `_SECTOR_ETF_MAP`, map it to a plausible sector ETF (Technology→XLK, Financials→XLF, Healthcare→XLV, Industrials→XLI, Consumer Discretionary→XLY, Consumer Staples→XLP, Energy→XLE, Utilities→XLU, Real Estate→XLRE, Materials→XLB, Communications→XLC).

Pragmatic approach: if you don't want to classify all ~200 missing symbols by hand, use `yfinance.Ticker(sym).info.get('sector')` in an offline script, generate a sector→ETF map, and paste the result. Or fall back to `"SPY"` for anything truly unknown.

For this task, the minimum acceptable change is:

```python
# Fallback: any symbol not in the explicit map gets SPY as a broad-market proxy
# so relative_strength and sector gates return SOMETHING rather than None.
def sector_etf_for(symbol: str) -> str:
    return _SECTOR_ETF_MAP.get(symbol, "SPY")
```

Then update callers that previously indexed `_SECTOR_ETF_MAP[sym]` or used `.get(sym)` directly to use `sector_etf_for(sym)`.

- [ ] **Step 6: Wire the sector gate into `generate_recommendations`**

Edit `src/scorched/services/recommender.py`. Find the existing max-position-pct gate loop (around line 595-622 per audit). Add the sector gate in the same loop:

```python
from ..services.research import sector_etf_for  # or wherever sector metadata lives
# ...
max_sector_pct = concentration.get("max_sector_pct", 40.0)
proposed_sector = rec_metadata.get(rec["symbol"], {}).get("sector")
if not check_sector_exposure(
    rec["symbol"], proposed_sector, dollar_amount,
    held_positions_list, total_value, max_sector_pct,
):
    reasons_rejected.append(f"{rec['symbol']}: exceeds {max_sector_pct:.0f}% sector cap")
    continue
```

The `held_positions_list` must include `sector` and `market_value` per position. Extend whichever builder constructs that list so the sector field is populated from `yfinance.Ticker.info` or a cached lookup (to avoid per-rec yfinance calls, cache in the Phase 0 research dict).

- [ ] **Step 7: Run tests to verify they pass**

Run: `docker compose exec tradebot pytest tests/test_sector_gate.py -v`
Expected: all PASS.

- [ ] **Step 8: Run the whole suite**

Run: `docker compose exec tradebot pytest -x -q`
Expected: green.

- [ ] **Step 9: Commit**

```bash
git add src/scorched/services/recommender.py src/scorched/services/research.py tests/test_sector_gate.py
git commit -m "feat: enforce max_sector_pct in code; expand sector ETF map to all S&P 500 pool"
```

---

## Task 10: Reconcile strategy.json with analyst_guidance.md

### Gap

- `strategy.json:6` — `"hold_period": "2-6wk"`
- `strategy.json:7-10` — `"entry_style": ["breakout", "mean_reversion"]`
- `analyst_guidance.md:13` — "short-term momentum (3–10 day holding period)"
- `analyst_guidance.md:116` — "Time stop at 7 days"
- `analyst_guidance.md:31` — "RSI <30 OVERSOLD — Wrong direction for momentum strategy. Avoid."
- `strategy.md` — repeats the 3-10d momentum story, with wrong numbers everywhere
- Memory says: "Strategy: Short-term momentum (3–10 days)"

Every Claude prompt injection reads both `strategy.json` (as structured prose) and `analyst_guidance.md` (the bigger, more detailed block). Claude gets contradictory instructions every day; the dashboard slider effectively does nothing.

### Decision made (Option B)

User elected **Option B**: keep `strategy.json` as-is (2–6 week holds, breakout + mean-reversion entries) and bring all markdown docs + prompts into alignment with it. Going forward, any strategy change via the dashboard must be mirrored into the md files in the same commit (saved to memory as `feedback_strategy_doc_sync.md`).

### What "consistent with strategy.json" means for each doc

`strategy.json` says: `hold_period = "2-6wk"`, `entry_style = ["breakout", "mean_reversion"]`, `max_position_pct = 20`, `max_sector_pct = 40`, `max_holdings = 5`.

Everywhere the docs currently say "3–10 day momentum" or "7-day time stop" or "RSI <30 is wrong direction" — rewrite. Mean-reversion specifically **wants** oversold setups; it's the opposite sign from momentum.

### Files
- Modify: `analyst_guidance.md` (rewrite framework from momentum → breakout/mean-reversion; adjust holding period, time stop, RSI interpretation, exit triggers)
- Modify: `strategy.md` (full rewrite OR replace with a "this file is reference only" banner pointing at strategy.json + analyst_guidance.md)
- Modify: `src/scorched/prompts/decision.md` (few-shot example uses a "3-10 day momentum" framing — rewrite to match)
- Modify: `src/scorched/CLAUDE.md` (the financial-analyst skill; it currently says "short-term momentum (3–10 day holding period)")
- No changes to: `strategy.json` (leave it)

### Steps

- [ ] **Step 1: Inventory the stale claims to fix**

Run:
```bash
cd /home/ubuntu/tradebot
grep -n '3[-–]10' analyst_guidance.md strategy.md src/scorched/prompts/*.md src/scorched/CLAUDE.md
grep -n '7[-– ]day\|7 trading day' analyst_guidance.md strategy.md src/scorched/prompts/*.md src/scorched/CLAUDE.md
grep -n -i 'momentum\|mean.reversion\|breakout' analyst_guidance.md strategy.md src/scorched/prompts/*.md src/scorched/CLAUDE.md
```

Keep the output — it's the list of lines you'll touch in later steps.

- [ ] **Step 2: Rewrite `analyst_guidance.md` for 2–6 week breakout + mean reversion**

Open `/home/ubuntu/tradebot/analyst_guidance.md`. Apply these specific edits (line numbers will shift as you go — search by text):

a. **Top framing** — change the phrase `short-term momentum (3–10 day holding period)` to:
```
swing / position trading with a 2–6 week holding period, targeting two complementary entry styles: (a) confirmed breakouts above technical resistance with volume expansion, and (b) mean-reversion entries on oversold pullbacks in uptrends.
```

b. **Price-momentum target range** — change `Target: +3% to +8%` (for week_change_pct, if present) to:
```
Target context: positive multi-week trend (ideally 4-week return > 0), with a near-term pullback or consolidation creating entry. Breakouts: stock clearing a prior resistance on >1.5× average volume. Mean-reversion: oversold within a confirmed uptrend (50-day MA still rising).
```

c. **RSI interpretation table** — replace the block that currently says:
```
RSI 40–65: healthy momentum zone, fine to enter.
RSI 65–70: approaching overbought but still tradeable if catalyst is strong.
RSI >70 (OVERBOUGHT): stock may be due for a pullback. Lower confidence, require stronger catalyst.
RSI <30 (OVERSOLD): wrong direction for momentum strategy — avoid unless specifically a mean-reversion play (which this strategy is not).
```
with:
```
RSI interpretation depends on entry style:
- **Breakout entry:** RSI 55–70 is ideal (momentum in the direction of the break); RSI >75 is stretched — prefer a pullback. RSI <45 on a "breakout" is suspect.
- **Mean-reversion entry:** RSI 25–40 is the target zone (oversold inside a larger uptrend). RSI <20 = catching a falling knife, wait for stabilisation. RSI >50 = not oversold, not a valid mean-reversion setup.
```

d. **Hard rules** — within the "Hard Rules" section:

- Change any "Time stop at 7 days" to:
  ```
  **Time stop at 30 calendar days (≈6 weeks of trading days).** If a position is flat or down after 30 calendar days with no fresh catalyst, exit regardless of thesis. Do not let a swing trade become a buy-and-hold.
  ```
- Change any "Stop loss at -5% from entry" to:
  ```
  **Stop loss at -8% from entry** (widened from -5% to accommodate 2–6 week volatility). Position sizing already scales for this wider stop (max 20% of portfolio). No averaging down.
  ```
- Change "No earnings holds (within 3 trading days)" to remain conceptually the same but reflect that a 2–6 week hold will often straddle at least one earnings print — add: `For 2–6 week holds that would span earnings, require the thesis to be earnings-independent or plan to trim 50% before the print.`
- Sector rule "No single sector may exceed 40%" — already matches strategy.json, leave intact.

e. **Exit-signal checklist** — rewrite the table to reflect the wider stops and longer holds:

| Exit Trigger | Action |
|-------------|--------|
| +15% gain within 2 weeks | Sell 50% (take partial, let rest run) |
| +25% gain at any time | Sell remainder |
| -8% from entry | Sell full position (hard stop) |
| 30 calendar days held, flat or down, no fresh catalyst | Sell full position (time stop) |
| Original catalyst invalidated (thesis broken) | Sell immediately |
| Earnings within 3 days + thesis is earnings-dependent | Sell before earnings |
| Sector rotation away from position's sector | Reduce or exit |
| SPY drops >3% intraday | Review all positions for exit |

f. **Common mistakes** — add a bullet: `**Confusing style mid-trade:** A position entered as breakout that becomes oversold is NOT a valid mean-reversion add-on. Pick a style at entry and stick with its exit rules.`

- [ ] **Step 3: Replace `strategy.md` with a human-readable reference**

Overwrite `/home/ubuntu/tradebot/strategy.md` with this short pointer file:

```markdown
# Scorched — Strategy Reference (human-readable)

> **This file is for human reference only. The bot does NOT read it.**
> Source of truth: `strategy.json` (edited via the dashboard at `/strategy`)
> and `analyst_guidance.md` (the framework the LLM is conditioned on).

## Current strategy snapshot

- **Horizon:** 2–6 week holds (swing/position)
- **Entry styles:** Breakout + Mean reversion
- **Sell discipline:** Scale out
- **Loss management:** Hybrid (time-based + price-based)
- **Position sizing:** 15–25% of portfolio per position, conviction-weighted
- **Cash floor:** 10% of total portfolio value (hard-enforced in code)
- **Max positions:** 5 simultaneous
- **Max sector exposure:** 40% of portfolio
- **Stop loss:** -8% from entry
- **Time stop:** 30 calendar days flat/down with no fresh catalyst

## How the bot applies this

Every morning the Phase 1 Claude analysis prompt receives:
1. The current `strategy.json` values, rendered into prose
2. The full `analyst_guidance.md` text as its framework

If the two disagree, Claude gets contradictory instructions and behaviour is unpredictable. Keep them in sync — see `feedback_strategy_doc_sync.md` in memory.
```

- [ ] **Step 4: Update `src/scorched/CLAUDE.md` (financial-analyst skill)**

Open `/home/ubuntu/tradebot/src/scorched/CLAUDE.md`. Apply analogous rewrites:

- Change `The declared strategy is **short-term momentum** (3–10 day holding period).` to:
  ```
  The declared strategy is **2–6 week swing/position trading** with two entry styles: **breakout** (confirmed range-clear on volume) and **mean reversion** (oversold pullbacks inside a confirmed uptrend).
  ```
- Rewrite the RSI block the same way as in `analyst_guidance.md` Step 2c.
- Update the "Max simultaneous positions" row from `3` to `5` (to match `strategy.json.concentration.max_holdings`).
- Update the time-stop rule from `7 days` to `30 calendar days`.
- Update stop-loss from `-5%` to `-8%`.
- Keep the tax-awareness and cash-floor sections — both still correct.

- [ ] **Step 5: Update the `decision.md` few-shot example**

Open `src/scorched/prompts/decision.md`. The Example 1 block uses CRWD as a 25-share buy with momentum framing (`Post-earnings breakout above $380 resistance on 3x volume`). That's actually compatible with breakout style — but the surrounding framing may reference a 3-10d horizon. Inspect + adjust so the example says something like:

> "Expected hold 2–4 weeks; trailing stop at -8% from entry; re-evaluate if breakout fails back under $380 within 5 sessions."

Also check that the `reasoning` and `key_risks` text doesn't reference a 7-day time stop.

- [ ] **Step 6: Sweep for any remaining `3-10`, `3–10`, or `7 day` stragglers**

Run:
```bash
cd /home/ubuntu/tradebot
grep -rn '3[-–]10\|7[- ]day\|7 trading' analyst_guidance.md strategy.md src/scorched/
```

Expected: no hits. If any remain, fix them in this same task rather than spinning a follow-up.

- [ ] **Step 7: Restart container (prompts are read at request time — no code change needed)**

Run: `cd /home/ubuntu/tradebot && docker compose restart tradebot`

Wait 10s, then confirm the strategy JSON unchanged:
```bash
curl -s http://127.0.0.1:8000/api/v1/strategy | python3 -c "import sys, json; d=json.load(sys.stdin); print('hold:', d.get('hold_period')); print('entry:', d.get('entry_style')); print('max_holdings:', d.get('concentration', {}).get('max_holdings'))"
```
Expected: `hold: 2-6wk`, `entry: ['breakout', 'mean_reversion']`, `max_holdings: 5`.

- [ ] **Step 8: Commit**

```bash
cd /home/ubuntu/tradebot
git add analyst_guidance.md strategy.md src/scorched/CLAUDE.md src/scorched/prompts/decision.md
git commit -m "docs: align analyst_guidance/strategy/decision prompt with strategy.json (2-6wk breakout+mean reversion)"
```

---

## Task 11: Drop `abs()` from momentum pre-filter scoring

### Gap

`src/scorched/services/research.py:843`:

```python
wk = abs(data.get("week_change_pct", 0))
```

`abs()` means a stock **down** 5% scores identically to one **up** 5% in the top-25 pre-filter that goes to Claude. For a momentum strategy that by construction will never buy a falling stock, this wastes pre-filter slots.

### Files
- Modify: `src/scorched/services/research.py:843` (one line)
- Test: `tests/test_research_scoring.py` (new file)

### Steps

- [ ] **Step 1: Read the function that contains the bug**

Run: `sed -n '830,870p' /home/ubuntu/tradebot/src/scorched/services/research.py`

Identify the enclosing function name (likely `_score_symbol`) and the surrounding scoring structure.

- [ ] **Step 2: Write failing test**

Create `tests/test_research_scoring.py`:

```python
"""_score_symbol must reward up-moves and penalise (or zero) down-moves."""


def test_score_symbol_rewards_positive_week_change():
    from scorched.services.research import _score_symbol

    data_up = {"week_change_pct": 5.0}  # healthy momentum
    data_down = {"week_change_pct": -5.0}  # wrong direction

    score_up = _score_symbol("UP", data_up)
    score_down = _score_symbol("DN", data_down)

    assert score_up > score_down, (
        f"Up move should outscore down move. up={score_up}, down={score_down}"
    )
```

- [ ] **Step 3: Run test to verify it fails**

Run: `docker compose exec tradebot pytest tests/test_research_scoring.py -v`

Expected: FAIL — `abs()` makes scores equal.

- [ ] **Step 4: Fix the scoring**

Edit `src/scorched/services/research.py:843` (or wherever `abs(...week_change_pct...)` appears inside `_score_symbol`). Replace:

```python
wk = abs(data.get("week_change_pct", 0))
```

with:

```python
wk = data.get("week_change_pct", 0) or 0
# Reward momentum in the declared direction; ignore or penalise wrong-direction moves
if wk < 0:
    wk = 0  # or, more aggressive: -wk * 0.5 penalty — keep simple for now
```

- [ ] **Step 5: Run test to verify it passes**

Run: `docker compose exec tradebot pytest tests/test_research_scoring.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/scorched/services/research.py tests/test_research_scoring.py
git commit -m "fix: drop abs() in momentum scoring so pre-filter rewards direction, not magnitude"
```

---

## Task 12: Use live prices for portfolio `total_value`

### Gap

`src/scorched/services/recommender.py:321-322`:

```python
portfolio_dict["total_value"] = cash + sum(
    Decimal(str(pos.avg_cost_basis)) * Decimal(str(pos.shares)) for pos in positions
)
```

`total_value` uses **cost basis**, not market value. It's consumed by:
- The 20%-of-portfolio max-position-size gate (`recommender.py:596-608`)
- The portfolio snapshot shown to Claude (Claude does its own math on it)

The drawdown gate separately uses live prices (`drawdown_gate.py:83-104`) — inconsistent.

Impact: in a winning portfolio (all positions up), the 20% gate under-sizes new buys.

### Files
- Modify: `src/scorched/services/recommender.py:321-322` (use live `price_data` already in scope)
- Test: add to existing `tests/test_drawdown_gate.py` or a new `tests/test_total_value.py`

### Steps

- [ ] **Step 1: Locate the build-up of `portfolio_dict` in `generate_recommendations`**

Run: `sed -n '300,340p' /home/ubuntu/tradebot/src/scorched/services/recommender.py`

Confirm `price_data` is already available in scope at that point (it should be — it's fetched before this block).

- [ ] **Step 2: Write failing test**

Create `tests/test_total_value.py`:

```python
"""Portfolio total_value must reflect live market prices, not cost basis."""
from decimal import Decimal
from types import SimpleNamespace


def test_portfolio_total_value_uses_live_prices():
    from scorched.services.recommender import _compute_portfolio_total_value

    positions = [
        SimpleNamespace(symbol="AAPL", shares=Decimal("10"), avg_cost_basis=Decimal("100")),
        SimpleNamespace(symbol="MSFT", shares=Decimal("5"), avg_cost_basis=Decimal("200")),
    ]
    cash = Decimal("5000")
    price_data = {
        "AAPL": {"current_price": Decimal("150")},  # +50%
        "MSFT": {"current_price": Decimal("180")},  # -10%
    }
    total = _compute_portfolio_total_value(cash, positions, price_data)
    # Expected: 5000 + 10*150 + 5*180 = 5000 + 1500 + 900 = 7400
    assert total == Decimal("7400")


def test_portfolio_total_value_falls_back_to_cost_basis_if_price_missing():
    from scorched.services.recommender import _compute_portfolio_total_value

    positions = [
        SimpleNamespace(symbol="WEIRD", shares=Decimal("10"), avg_cost_basis=Decimal("50")),
    ]
    cash = Decimal("1000")
    price_data = {}  # No live price
    total = _compute_portfolio_total_value(cash, positions, price_data)
    # Fall back: 1000 + 10*50 = 1500
    assert total == Decimal("1500")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `docker compose exec tradebot pytest tests/test_total_value.py -v`

Expected: FAIL — helper doesn't exist.

- [ ] **Step 4: Extract the total_value calculation into a helper**

Edit `src/scorched/services/recommender.py`. Before `generate_recommendations`, add:

```python
def _compute_portfolio_total_value(
    cash: Decimal, positions, price_data: dict
) -> Decimal:
    """Cash + sum of (live_price * shares), falling back to avg_cost_basis if no live price."""
    total = cash
    for pos in positions:
        live = (price_data or {}).get(pos.symbol, {}).get("current_price")
        price = Decimal(str(live)) if live else Decimal(str(pos.avg_cost_basis))
        total += price * Decimal(str(pos.shares))
    return total
```

Then in `generate_recommendations` (line 321-322), replace:

```python
portfolio_dict["total_value"] = cash + sum(
    Decimal(str(pos.avg_cost_basis)) * Decimal(str(pos.shares)) for pos in positions
)
```

with:

```python
portfolio_dict["total_value"] = _compute_portfolio_total_value(cash, positions, price_data)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker compose exec tradebot pytest tests/test_total_value.py -v`
Expected: PASS.

- [ ] **Step 6: Run full test suite**

Run: `docker compose exec tradebot pytest -x -q`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add src/scorched/services/recommender.py tests/test_total_value.py
git commit -m "fix: portfolio total_value uses live prices, not cost basis (affects position-size gate)"
```

---

## Task 13: Bound Phase 0 `asyncio.gather` with a timeout

### Gap

`src/scorched/api/prefetch.py:107-119` (approximate line range — verify with grep):

```python
results = await asyncio.gather(
    fred_task, insider_task, av_task, twelvedata_task, alpaca_news_task, ...,
    return_exceptions=True,
)
```

No timeout. One stuck socket (FRED DNS, Alpha Vantage throttle, Twelvedata regional outage) can hang the whole Phase 0 indefinitely. The "SLOW" warning at `prefetch.py:213` is post-hoc.

### Files
- Modify: `src/scorched/api/prefetch.py`
- Test: `tests/test_prefetch_timeout.py` (new file)

### Steps

- [ ] **Step 1: Locate the gather**

Run: `grep -n 'asyncio.gather' /home/ubuntu/tradebot/src/scorched/api/prefetch.py`

Note the exact line and surrounding code block.

- [ ] **Step 2: Write a test that simulates a hung fetch**

Create `tests/test_prefetch_timeout.py`:

```python
"""Phase 0 gather must be bounded so one hung source can't hang the whole phase."""
import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_prefetch_bounds_hung_task(monkeypatch):
    """If one fetch coroutine hangs, the gather must time out and return."""
    from scorched.api import prefetch

    # Patch a single fetcher to hang forever
    async def hang(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(prefetch, "fetch_fred_macro", hang, raising=False)
    monkeypatch.setattr(prefetch, "PHASE0_GATHER_TIMEOUT_S", 2)

    start = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        # Call the wrapper, not the whole endpoint. Adjust function name as needed.
        await prefetch._gather_with_timeout([hang()], timeout_s=2)
    elapsed = time.monotonic() - start
    assert elapsed < 5, f"Should have timed out in ~2s, took {elapsed:.1f}s"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `docker compose exec tradebot pytest tests/test_prefetch_timeout.py -v`

Expected: FAIL — `_gather_with_timeout` and `PHASE0_GATHER_TIMEOUT_S` don't exist.

- [ ] **Step 4: Add timeout wrapper to prefetch.py**

Edit `src/scorched/api/prefetch.py`. Near the top, add:

```python
PHASE0_GATHER_TIMEOUT_S = 600  # 10 min hard cap on the parallel fetch block


async def _gather_with_timeout(tasks, timeout_s: float):
    """Like asyncio.gather(..., return_exceptions=True) but bounded.

    On timeout, logs which tasks were still pending so operators know what hung.
    Raises asyncio.TimeoutError.
    """
    try:
        return await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        # Cancel any lingering tasks and report
        for t in tasks:
            if hasattr(t, "get_name") and not t.done():
                logger.warning("Phase 0 timeout: task %s still pending", t.get_name())
        raise
```

Replace the existing `await asyncio.gather(...)` call in Phase 0 with:

```python
results = await _gather_with_timeout(all_tasks, timeout_s=PHASE0_GATHER_TIMEOUT_S)
```

Catch the `asyncio.TimeoutError` higher up and convert it into a 504-ish response + Telegram alert, keeping whatever partial results were already on disk:

```python
try:
    results = await _gather_with_timeout(all_tasks, timeout_s=PHASE0_GATHER_TIMEOUT_S)
except asyncio.TimeoutError:
    await send_telegram(f"TRADEBOT // Phase 0 hit {PHASE0_GATHER_TIMEOUT_S}s hard timeout")
    raise HTTPException(status_code=504, detail="Phase 0 exceeded hard timeout")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker compose exec tradebot pytest tests/test_prefetch_timeout.py -v`
Expected: PASS.

- [ ] **Step 6: Rebuild and sanity-check timing**

Run: `cd /home/ubuntu/tradebot && docker compose up -d --build tradebot`

Hit the endpoint with the real data:
```bash
time curl -s -X POST -H "X-Owner-Pin: $(grep '^SETTINGS_PIN=' .env | cut -d= -f2)" http://127.0.0.1:8000/api/v1/research/prefetch -d '{}' >/dev/null
```

Expected: completes in ~200s normally. No new regression.

- [ ] **Step 7: Commit**

```bash
git add src/scorched/api/prefetch.py tests/test_prefetch_timeout.py
git commit -m "fix: bound Phase 0 asyncio.gather with 600s hard timeout so hung sources don't hang the phase"
```

---

## Task 14: Bump Phase 3 EOD timeout + Telegram alert on failure

### Gap

`cron/tradebot_phase3.py:144-145`:

```python
except Exception as e:
    print(f"EOD review error (non-fatal): {e}")
```

`http_post` for `/api/v1/market/eod-review` uses default `timeout=60` (client-side socket). Claude's own timeout is 300s, so the client gives up well before Claude does. Today's `cron.log`: `EOD review error (non-fatal): timed out`. No Telegram. Playbook did not update.

### Files
- Modify: `cron/tradebot_phase3.py`

### Steps

- [ ] **Step 1: Locate the EOD call in phase3**

Run: `grep -n 'eod-review\|market/eod' /home/ubuntu/tradebot/cron/tradebot_phase3.py`

Note the line number of the `http_post(...)` call.

- [ ] **Step 2: Bump timeout and route errors to Telegram**

Edit `cron/tradebot_phase3.py`. Find the existing block:

```python
try:
    http_post("/api/v1/market/eod-review", {...})
except Exception as e:
    print(f"EOD review error (non-fatal): {e}")
```

Replace with:

```python
try:
    http_post("/api/v1/market/eod-review", {...}, timeout=600)
except Exception as e:
    msg = f"TRADEBOT // Phase 3 EOD review failed: {e}"
    print(msg)
    send_telegram(msg)
```

(Use the exact variable names from the existing code — this snippet is a pattern, not verbatim.)

- [ ] **Step 3: Also bump weekly-reflection and playbook-update timeouts if they use defaults**

Run: `grep -n 'http_post' /home/ubuntu/tradebot/cron/tradebot_phase3.py`

For any POST that targets a Claude-backed endpoint (EOD review, playbook update, weekly reflection), pass `timeout=600`. For pure DB reads, leave the default.

- [ ] **Step 4: Dry-run against a manual trigger (optional — Phase 3 only meaningful after market close)**

Skip or run locally with mock. The change is small and the risk is low.

- [ ] **Step 5: Commit**

```bash
git add cron/tradebot_phase3.py
git commit -m "fix: Phase 3 EOD review gets 600s timeout + Telegram alert on failure"
```

---

## Task 15: Redact API keys from `api_call_log` error messages

### Gap

`src/scorched/api_tracker.py:86`:

```python
error_message = str(exc)[:500]
```

External API failures (Alpha Vantage, Twelvedata, FRED, Finnhub, Polygon-legacy) often produce exceptions whose `str()` includes the full URL — with the API key in the query string. That URL lands in `api_call_log` and is rendered on `/api/v1/system/errors`, exposing keys in the dashboard.

Telegram code (`services/telegram.py:32-33`) handles this correctly with an explicit "don't log URL" comment. The pattern needs to apply to the tracker too.

### Files
- Modify: `src/scorched/api_tracker.py` (add redaction before storage)
- Test: `tests/test_api_tracker_redaction.py` (new file — complements existing `test_api_tracker.py`)

### Steps

- [ ] **Step 1: Write failing test**

Create `tests/test_api_tracker_redaction.py`:

```python
"""api_tracker must redact common API-key patterns before storing error_message."""


def test_redact_apikey_query_param():
    from scorched.api_tracker import _redact_secrets

    msg = "HTTPError: https://www.alphavantage.co/query?function=RSI&symbol=AAPL&apikey=REALKEY123 500 Server Error"
    out = _redact_secrets(msg)
    assert "REALKEY123" not in out
    assert "apikey=REDACTED" in out or "apikey=***" in out


def test_redact_api_key_underscore_variant():
    from scorched.api_tracker import _redact_secrets
    assert "REALKEY" not in _redact_secrets("bad url ?api_key=REALKEY&foo=bar")


def test_redact_token_query_param():
    from scorched.api_tracker import _redact_secrets
    assert "REALTOKEN" not in _redact_secrets("https://finnhub.io/api/v1/stock/recommendation?symbol=AAPL&token=REALTOKEN")


def test_redact_telegram_bot_path():
    from scorched.api_tracker import _redact_secrets
    assert "1234567:REAL_TOKEN" not in _redact_secrets(
        "POST https://api.telegram.org/bot1234567:REAL_TOKEN/sendMessage failed"
    )


def test_redact_is_idempotent():
    from scorched.api_tracker import _redact_secrets
    once = _redact_secrets("?apikey=X")
    twice = _redact_secrets(once)
    assert once == twice
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec tradebot pytest tests/test_api_tracker_redaction.py -v`
Expected: FAIL — `_redact_secrets` doesn't exist.

- [ ] **Step 3: Implement `_redact_secrets`**

Edit `src/scorched/api_tracker.py`. Add near the top (after imports):

```python
import re

# Patterns for URL-embedded secrets: apikey, api_key, token, apiKey, and
# Telegram's bot<token>/path form.
_SECRET_PATTERNS = [
    re.compile(r"(?i)(apikey|api_key|apikey)\s*=\s*[^\s&#]+"),
    re.compile(r"(?i)token\s*=\s*[^\s&#]+"),
    re.compile(r"/bot\d+:[A-Za-z0-9_-]+/"),
]


def _redact_secrets(text: str) -> str:
    """Strip common API-key and token patterns from a string."""
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: _mask(m.group(0)), out)
    return out


def _mask(fragment: str) -> str:
    """Given 'apikey=SECRET' return 'apikey=REDACTED'. For '/botNNN:TOK/' return '/bot***/'."""
    if fragment.startswith("/bot"):
        return "/bot***/"
    if "=" in fragment:
        k, _, _v = fragment.partition("=")
        return f"{k}=REDACTED"
    return "REDACTED"
```

Then update the `record_usage` / `_record_call` flow where `error_message` is set. Find:

```python
error_message = str(exc)[:500]
```

Replace with:

```python
error_message = _redact_secrets(str(exc))[:500]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec tradebot pytest tests/test_api_tracker_redaction.py -v`
Expected: PASS.

- [ ] **Step 5: Run the existing tracker test suite for regressions**

Run: `docker compose exec tradebot pytest tests/test_api_tracker.py -v`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/scorched/api_tracker.py tests/test_api_tracker_redaction.py
git commit -m "fix: redact API keys/tokens from api_call_log error_message before storage"
```

---

## Task 16: Startup assertion for live-mode + weak PIN

### Gap

`src/scorched/api/deps.py:12` treats empty `settings.settings_pin` as "auth disabled". Intentional for local dev. But the trap is going live (`BROKER_MODE=alpaca_live`) while forgetting to set a PIN — the entire mutation surface becomes unauthenticated against the real Alpaca account. No startup guard exists today.

### Files
- Modify: `src/scorched/main.py` (add assertion in `lifespan`)
- Test: `tests/test_startup_assertion.py` (new file)

### Steps

- [ ] **Step 1: Locate the lifespan function**

Run: `grep -n 'lifespan\|async def ' /home/ubuntu/tradebot/src/scorched/main.py | head -20`

Identify the async lifespan function (likely decorated with `@asynccontextmanager` or assigned to `app = FastAPI(lifespan=...)`).

- [ ] **Step 2: Write failing test**

Create `tests/test_startup_assertion.py`:

```python
"""main.py must refuse to boot when broker_mode is live and the PIN is too weak."""
import pytest


def test_startup_refuses_live_mode_with_empty_pin(monkeypatch):
    from scorched import main as main_mod

    monkeypatch.setattr(main_mod.settings, "broker_mode", "alpaca_live")
    monkeypatch.setattr(main_mod.settings, "settings_pin", "")

    with pytest.raises(RuntimeError, match="SETTINGS_PIN"):
        main_mod._assert_live_mode_safe()


def test_startup_refuses_live_mode_with_short_pin(monkeypatch):
    from scorched import main as main_mod

    monkeypatch.setattr(main_mod.settings, "broker_mode", "alpaca_live")
    monkeypatch.setattr(main_mod.settings, "settings_pin", "1234")

    with pytest.raises(RuntimeError, match="at least 16"):
        main_mod._assert_live_mode_safe()


def test_startup_allows_paper_mode_with_any_pin(monkeypatch):
    from scorched import main as main_mod

    monkeypatch.setattr(main_mod.settings, "broker_mode", "paper")
    monkeypatch.setattr(main_mod.settings, "settings_pin", "")
    # Should not raise
    main_mod._assert_live_mode_safe()


def test_startup_allows_live_mode_with_strong_pin(monkeypatch):
    from scorched import main as main_mod

    monkeypatch.setattr(main_mod.settings, "broker_mode", "alpaca_live")
    monkeypatch.setattr(main_mod.settings, "settings_pin", "X" * 20)
    main_mod._assert_live_mode_safe()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `docker compose exec tradebot pytest tests/test_startup_assertion.py -v`
Expected: FAIL — `_assert_live_mode_safe` doesn't exist.

- [ ] **Step 4: Implement the assertion and wire it into lifespan**

Edit `src/scorched/main.py`. Add near the top (after `from .config import settings`):

```python
MIN_LIVE_PIN_LEN = 16


def _assert_live_mode_safe() -> None:
    """Refuse to boot in live broker mode without a strong PIN."""
    if settings.broker_mode in ("alpaca_live",):
        pin = settings.settings_pin or ""
        if not pin:
            raise RuntimeError(
                "SETTINGS_PIN is unset while BROKER_MODE=alpaca_live — refusing to start"
            )
        if len(pin) < MIN_LIVE_PIN_LEN:
            raise RuntimeError(
                f"SETTINGS_PIN is too short (len {len(pin)}) for live mode — "
                f"need at least {MIN_LIVE_PIN_LEN} characters"
            )
```

Inside the `lifespan` function, call it before anything else:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    _assert_live_mode_safe()
    # ... rest of existing startup ...
    yield
    # ... shutdown ...
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker compose exec tradebot pytest tests/test_startup_assertion.py -v`
Expected: PASS.

- [ ] **Step 6: Full suite regression check**

Run: `docker compose exec tradebot pytest -x -q`
Expected: green.

- [ ] **Step 7: Rebuild + smoke-test (we're in paper mode so boot should succeed)**

Run: `cd /home/ubuntu/tradebot && docker compose up -d --build tradebot`
Wait 10s, then: `docker compose logs tradebot | tail -20`
Expected: startup completes cleanly (no `RuntimeError`).

- [ ] **Step 8: Commit**

```bash
git add src/scorched/main.py tests/test_startup_assertion.py
git commit -m "feat: refuse to boot in alpaca_live mode without a 16+ char SETTINGS_PIN"
```

---

## Final integration check

After all 16 tasks are committed:

- [ ] **A. Full test suite**

Run: `cd /home/ubuntu/tradebot && docker compose exec tradebot pytest -x -q`
Expected: all green. ~120+ tests.

- [ ] **B. Manual round-trip: hit the happy path through the API**

```bash
PIN=$(grep '^SETTINGS_PIN=' .env | cut -d= -f2)
curl -s http://127.0.0.1:8000/api/v1/system/health | jq '.status'
curl -s -H "X-Owner-Pin: $PIN" http://127.0.0.1:8000/api/v1/onboarding/status | jq '.broker_mode'
curl -s http://127.0.0.1:8000/api/v1/portfolio | jq '.cash_balance, .total_value'
```

- [ ] **C. Dry-run each cron phase**

```bash
. /home/ubuntu/.tradebot_cron_env
cd /home/ubuntu/tradebot
python3 cron/tradebot_phase1_5.py 2>&1 | tail -20   # must not say "disabled"
python3 cron/intraday_monitor.py 2>&1 | tail -20    # must not silently skip on stale lock
```

- [ ] **D. Verify `.env` permissions**

Run: `stat -c '%a %n' /home/ubuntu/tradebot/.env /home/ubuntu/.tradebot_cron_env`
Expected: `600 /home/ubuntu/tradebot/.env` and `600 /home/ubuntu/.tradebot_cron_env`.

- [ ] **E. Update CLAUDE.md memory**

After this plan is fully executed, the "Gotchas" section of the top-level `CLAUDE.md` will be partially stale (circuit breaker now enabled, Alpaca news wired, trailing stops live, etc.). That's a separate documentation sweep — not in scope for this remediation plan.

---

## Self-review against scope

**Coverage:** each of the 16 items in the "Gaps being closed" table has a dedicated task. ✅

**Placeholders:** no TBDs, no "handle edge cases", no "similar to Task N". ✅

**Type consistency:** `_redact_secrets`, `_assert_live_mode_safe`, `check_sector_exposure`, `_compute_portfolio_total_value`, `update_trailing_stop`, `_gather_with_timeout` — each is defined in one task and referenced consistently. ✅

**Known dependencies:**
- Task 6 + 7 share the `build_research_context` signature; if both are worked in parallel, do 7 after 6 (or rebase).
- Task 8 + 9 both touch `recommender.py` gate loop and `research.py` sector map; do 8 then 9, or review the combined diff before committing.
- Task 2 should land before Task 5 exercises its lock under load, but order doesn't matter for correctness.
