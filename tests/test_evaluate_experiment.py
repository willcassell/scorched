"""Tests for scripts/evaluate_experiment.py's pure verdict logic.

The script itself hits the live DB, Alpaca, and Telegram — not unit-testable
without heavy mocking. `compute_verdict()` was extracted specifically so the
KEEP/RETIRE decision math and message formatting can be tested directly,
without any I/O. `scripts/` has no `__init__.py` (it's a bag of CLI entry
points, not an importable package), so we load the module by file path.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "evaluate_experiment.py"

_spec = importlib.util.spec_from_file_location("evaluate_experiment", _SCRIPT_PATH)
evaluate_experiment = importlib.util.module_from_spec(_spec)
sys.modules["evaluate_experiment"] = evaluate_experiment
_spec.loader.exec_module(evaluate_experiment)

compute_verdict = evaluate_experiment.compute_verdict
_parse_date = evaluate_experiment._parse_date
missing_experiment_fields = evaluate_experiment.missing_experiment_fields


def _base_kwargs(**overrides) -> dict:
    kwargs = dict(
        name="C-best-in-class",
        start_date=date(2026, 8, 3),
        deadline_date=date(2026, 10, 27),
        baseline_portfolio=Decimal("101798.53"),
        baseline_spy=Decimal("746.79"),
        current_portfolio=Decimal("105000.00"),
        current_spy=Decimal("760.00"),
        pf=Decimal("1.5"),
        n_sells=10,
        wins=Decimal("3000"),
        losses=Decimal("2000"),
        today=date(2026, 9, 1),
    )
    kwargs.update(overrides)
    return kwargs


def test_keep_when_beats_spy_and_pf_above_one():
    result = compute_verdict(**_base_kwargs())
    assert result["beats_spy"] is True
    assert result["pf_ok"] is True
    assert result["keep"] is True
    assert result["verdict"] == "KEEP (criterion met)"
    assert "KEEP" in result["message"]


def test_retire_when_pf_below_one_even_if_beats_spy():
    result = compute_verdict(**_base_kwargs(pf=Decimal("0.8")))
    assert result["beats_spy"] is True
    assert result["pf_ok"] is False
    assert result["keep"] is False
    assert result["verdict"] == "RETIRE"


def test_retire_when_trails_spy_even_if_pf_above_one():
    # Portfolio +3.15% vs SPY +12% — beats_spy should be False.
    result = compute_verdict(**_base_kwargs(current_spy=Decimal("836.40")))
    assert result["beats_spy"] is False
    assert result["pf_ok"] is True
    assert result["keep"] is False
    assert result["verdict"] == "RETIRE"


def test_spy_unavailable_fails_closed():
    """A failed Alpaca snapshot must not silently pass the beats-SPY check."""
    result = compute_verdict(**_base_kwargs(current_spy=None))
    assert result["spy_ret"] is None
    assert result["beats_spy"] is False
    assert result["keep"] is False
    assert "UNAVAILABLE" in result["message"]


def test_early_eval_is_informational_not_actionable():
    result = compute_verdict(**_base_kwargs(pf=Decimal("0.5"), today=date(2026, 8, 15)))
    assert result["on_time"] is False
    assert result["keep"] is False
    assert "EARLY, informational" in result["message"]
    assert "ACTION: criterion failed" not in result["message"]


def test_on_time_failure_includes_action_line():
    result = compute_verdict(**_base_kwargs(pf=Decimal("0.5"), today=date(2026, 10, 27)))
    assert result["on_time"] is True
    assert result["keep"] is False
    assert "ACTION: criterion failed at the deadline" in result["message"]


def test_message_names_the_experiment():
    result = compute_verdict(**_base_kwargs(name="C-best-in-class"))
    assert "EXPERIMENT C-best-in-class VERDICT" in result["message"]


def test_parse_date_roundtrip():
    assert _parse_date("2026-08-03") == date(2026, 8, 3)


def test_missing_experiment_fields_complete_block():
    complete = {
        "name": "C-best-in-class",
        "start_date": "2026-08-03",
        "deadline_approx_date": "2026-10-27",
        "baseline_portfolio_value": 101798.53,
        "baseline_spy": 746.79,
    }
    assert missing_experiment_fields(complete) == []


def test_missing_experiment_fields_reports_each_gap():
    incomplete = {"name": "D-whatever", "start_date": "2027-01-05"}
    missing = missing_experiment_fields(incomplete)
    assert "deadline_approx_date" in missing
    assert "baseline_portfolio_value" in missing
    assert "baseline_spy" in missing
    assert "start_date" not in missing


def test_missing_experiment_fields_empty_block():
    assert missing_experiment_fields({}) == list(evaluate_experiment._REQUIRED_EXPERIMENT_FIELDS)


def test_exact_tie_does_not_beat_spy():
    """Portfolio return exactly equal to SPY's does not count as beating it —
    the comparison is strict '>' per the pre-committed kill criterion text."""
    result = compute_verdict(**_base_kwargs(
        current_portfolio=Decimal("101798.53"),  # 0% portfolio return
        baseline_spy=Decimal("746.79"),
        current_spy=Decimal("746.79"),  # 0% SPY return too — exact tie
    ))
    assert result["port_ret"] == 0
    assert result["beats_spy"] is False
