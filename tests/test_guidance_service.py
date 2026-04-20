"""Unit tests for the guidance service layer and linter."""
from __future__ import annotations

from scorched.services.guidance import (
    parse_hard_rules,
    render_rule_overrides_addendum,
)


SAMPLE_GUIDANCE = """# Analyst Guidance

Some preamble.

## Hard Rules — Never Break

1. **Catalyst required**: Do not recommend without a catalyst.
2. **No earnings risk**: Do not open a new position within 3 trading days of earnings.
3. **Sector limit**: No single sector may exceed 40% of total value.

---

## Other section
Blah.
"""


def test_parse_hard_rules_extracts_three():
    rules = parse_hard_rules(SAMPLE_GUIDANCE)
    assert [r.number for r in rules] == [1, 2, 3]
    assert rules[0].title.startswith("Catalyst")
    assert "40%" in rules[2].body


def test_parse_hard_rules_handles_missing_section():
    assert parse_hard_rules("# no hard rules here\n\njust prose") == []


def test_parse_hard_rules_applies_overrides_to_toggle_rules_only():
    rules = parse_hard_rules(
        SAMPLE_GUIDANCE,
        overrides={"earnings_blackout": {"enabled": False}},
    )
    by_num = {r.number: r for r in rules}
    # Rule 1 is not wired — overrides must be None
    assert by_num[1].overrides is None
    # Rule 2 is wired — override dict surfaces
    assert by_num[2].overrides == {"enabled": False}


def test_rule_overrides_addendum_empty_when_no_overrides():
    assert render_rule_overrides_addendum({}) == ""
    assert render_rule_overrides_addendum({"rule_overrides": {}}) == ""


def test_rule_overrides_addendum_silent_on_all_defaults():
    # Every toggle at its default → addendum should be empty (nothing to tell
    # Claude). This is the baseline behavior preservation contract.
    strat = {"rule_overrides": {
        "earnings_blackout": {"enabled": True, "days": 3, "existing_position_action": "trim_50pct"},
        "gain_trigger": {"enabled": True, "threshold_pct": 100},
        "selloff_threshold": {"enabled": True, "spy_drop_pct": 2.0},
    }}
    assert render_rule_overrides_addendum(strat) == ""


def test_rule_overrides_addendum_speaks_up_when_disabled():
    out = render_rule_overrides_addendum({"rule_overrides": {
        "earnings_blackout": {"enabled": False},
    }})
    assert "DISABLED" in out
    assert "Rule #2" in out
    assert "LIVE RULE OVERRIDES" in out


def test_rule_overrides_addendum_speaks_up_when_number_changed():
    out = render_rule_overrides_addendum({"rule_overrides": {
        "earnings_blackout": {"enabled": True, "days": 5, "existing_position_action": "exit"},
    }})
    assert "5 trading days" in out
    assert "fully exit" in out


def test_rule_overrides_addendum_stacks_multiple():
    out = render_rule_overrides_addendum({"rule_overrides": {
        "earnings_blackout": {"enabled": False},
        "gain_trigger": {"enabled": True, "threshold_pct": 150},
        "selloff_threshold": {"enabled": True, "spy_drop_pct": 3.0},
    }})
    assert "Rule #2" in out and "DISABLED" in out
    assert "Rule #6" in out and "150%" in out
    assert "Rule #7" in out and "3.0%" in out


def test_linter_module_importable_and_returns_findings():
    from scorched.services import guidance_lint

    findings = guidance_lint.lint(
        strategy_json={"concentration": {"max_sector_pct": 40}},
        guidance_text=SAMPLE_GUIDANCE,
    )
    assert findings
    # The sector_limit check should report ok since both say 40%
    sector = next(f for f in findings if f.check == "sector_limit")
    assert sector.severity == "ok"


def test_linter_catches_sector_mismatch():
    from scorched.services import guidance_lint

    findings = guidance_lint.lint(
        strategy_json={"concentration": {"max_sector_pct": 50}},  # out of sync
        guidance_text=SAMPLE_GUIDANCE,  # says 40% in the Hard Rules
    )
    sector = next(f for f in findings if f.check == "sector_limit")
    assert sector.severity == "error"
    assert "50" in sector.strategy_value
    assert "40" in sector.guidance_value
