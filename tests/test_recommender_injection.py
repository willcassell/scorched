"""Guards the prompt-injection contract between strategy.json.rule_overrides
and the morning recommendation flow.

We don't run the full make_recommendations flow (it fetches research data,
talks to Claude, writes to the DB). Instead we cover two layers that
compose end-to-end:

1. services.guidance.load_effective_guidance() — produces the exact bytes
   that land in `guidance` inside make_recommendations. Direct unit tests.
2. recommender.py's call sites — verify the import binding still exists
   and points at the helper, so a future refactor can't silently sever
   the wiring without the test noticing.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def temp_strategy(monkeypatch, tmp_path):
    """Point the strategy loader at a temp strategy.json so tests can
    mutate it freely without touching the real file.
    """
    strategy_file = tmp_path / "strategy.json"
    # Seed with a minimal valid strategy — the loader accepts any JSON.
    strategy_file.write_text(json.dumps({
        "objective": "growth",
        "hold_period": "2-6wk",
        "concentration": {"max_position_pct": 25, "max_sector_pct": 40, "max_holdings": 8},
    }))
    from scorched.services import strategy as strategy_mod
    monkeypatch.setattr(strategy_mod, "_REPO_ROOT", tmp_path)
    # guidance.py caches the repo root at import time from strategy.py;
    # reach into the same module to repoint there.
    from scorched.services import guidance as guidance_mod
    monkeypatch.setattr(guidance_mod, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(guidance_mod, "_GUIDANCE_PATH", tmp_path / "analyst_guidance.md")

    # Minimal guidance file so load_analyst_guidance returns non-empty.
    (tmp_path / "analyst_guidance.md").write_text(
        "# Analyst Guidance\n\n"
        "## Hard Rules — Never Break\n\n"
        "1. **Catalyst required**: name one.\n"
        "2. **No earnings risk**: Do not open a new position if the company reports earnings within 3 trading days.\n"
    )
    return strategy_file


def _set_override(strategy_path: Path, **overrides):
    data = json.loads(strategy_path.read_text())
    data["rule_overrides"] = overrides
    strategy_path.write_text(json.dumps(data))


def test_effective_guidance_baseline_matches_raw(temp_strategy):
    """No overrides set → effective guidance equals raw file content."""
    from scorched.services.guidance import load_effective_guidance
    from scorched.services.strategy import load_analyst_guidance

    out = load_effective_guidance()
    raw = load_analyst_guidance()
    assert out == raw, "baseline must be byte-for-byte identical to raw"
    assert "LIVE RULE OVERRIDES" not in out


def test_effective_guidance_appends_addendum_when_rule_disabled(temp_strategy):
    """Toggle earnings_blackout off → addendum appears after raw guidance."""
    _set_override(temp_strategy, earnings_blackout={"enabled": False})

    from scorched.services.guidance import load_effective_guidance
    out = load_effective_guidance()

    assert "LIVE RULE OVERRIDES" in out
    assert "Rule #2" in out
    assert "DISABLED" in out
    # Crucial: the addendum must come AFTER the raw content, not replace it.
    assert out.index("Hard Rules — Never Break") < out.index("LIVE RULE OVERRIDES")


def test_effective_guidance_appends_addendum_when_number_changed(temp_strategy):
    """Bump earnings window to 5 days → addendum spells out the override."""
    _set_override(temp_strategy, earnings_blackout={
        "enabled": True, "days": 5, "existing_position_action": "exit",
    })
    from scorched.services.guidance import load_effective_guidance
    out = load_effective_guidance()
    assert "LIVE RULE OVERRIDES" in out
    assert "5 trading days" in out
    assert "fully exit" in out


def test_effective_guidance_stacks_multiple_overrides(temp_strategy):
    _set_override(temp_strategy,
                  earnings_blackout={"enabled": False},
                  gain_trigger={"enabled": True, "threshold_pct": 150},
                  selloff_threshold={"enabled": True, "spy_drop_pct": 3.0})
    from scorched.services.guidance import load_effective_guidance
    out = load_effective_guidance()
    # All three rules mentioned in the addendum, in rule-number order.
    rule2_idx = out.index("Rule #2")
    rule6_idx = out.index("Rule #6")
    rule7_idx = out.index("Rule #7")
    assert rule2_idx < rule6_idx < rule7_idx
    assert "DISABLED" in out
    assert "150%" in out
    assert "3.0%" in out


def test_recommender_calls_load_effective_guidance():
    """The recommender must use the helper, not construct the addendum
    inline. Guards against a refactor accidentally bypassing the helper
    and losing the override-injection path."""
    source = (_REPO_ROOT / "src/scorched/services/recommender.py").read_text()
    # Exactly one usage of the helper in the recommender. If this fails,
    # someone added a second call site — confirm it's intentional, then
    # bump the assertion.
    assert source.count("load_effective_guidance()") == 1
    # And no stale direct addendum rendering inline.
    assert "render_rule_overrides_addendum(" not in source, \
        "recommender should go through load_effective_guidance, not call the renderer directly"


def test_playbook_calls_build_overrides_addendum():
    """The EOD playbook update must also see overrides. The playbook uses
    build_overrides_addendum() because it extracts Hard Rules separately
    (the extractor regex terminates on the addendum's ## heading)."""
    source = (_REPO_ROOT / "src/scorched/services/playbook.py").read_text()
    assert "build_overrides_addendum()" in source
    assert "render_rule_overrides_addendum(" not in source, \
        "playbook should use build_overrides_addendum(), not the raw renderer"


@pytest.mark.asyncio
async def test_call_analysis_receives_guidance_with_addendum_when_toggled(
    temp_strategy, monkeypatch,
):
    """Integration smoke: patch call_analysis, assert the guidance string
    passed to it contains LIVE RULE OVERRIDES when a toggle is flipped.

    We call load_effective_guidance directly (it's the same function
    recommender.py invokes two lines after load_strategy()), then pass
    its return into a mocked call_analysis — a minimal reproduction of
    the real code path without the fixture surface of the full flow.
    """
    _set_override(temp_strategy, earnings_blackout={"enabled": False})

    from scorched.services.guidance import load_effective_guidance
    # Mock the Claude call as the recommender uses it.
    seen = {}

    async def fake_call_analysis(strategy, guidance, user_msg, tracker=None):
        seen["guidance"] = guidance
        return (None, "", "", [], [])

    # The recommender imports call_analysis from claude_client. Patch there.
    from scorched.services import claude_client
    monkeypatch.setattr(claude_client, "call_analysis", fake_call_analysis)

    # Simulate the two lines in make_recommendations that matter for this test.
    guidance = load_effective_guidance()
    await claude_client.call_analysis("strategy prose", guidance, "user msg")

    assert "LIVE RULE OVERRIDES" in seen["guidance"]
    assert "Rule #2" in seen["guidance"]
    assert "DISABLED" in seen["guidance"]
