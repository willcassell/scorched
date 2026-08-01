"""Tests for Task 7: trailing-stop params configurable + stale sizing fallbacks.

Covers:
  1. `update_trailing_stop` / `compute_trailing_stop` forward custom
     atr_multiplier / min_stop_pct through to the ratchet formula, and
     the pure-function defaults (2.0 / 5.0) stay unchanged.
  2. `initial_stop_price` — the day-one fixed-pct floor a brand-new
     position gets before any ATR/HWM history exists — is configurable
     via the same min_stop_pct knob.
  3. The stale `max_position_pct` fallback (was hardcoded 33 at five
     call sites) is centralized at 15 in `risk_gates.DEFAULT_MAX_POSITION_PCT`
     and no call site still hardcodes 33.
  4. `strategy.py`'s DEFAULT_JSON fallback mirrors the live strategy.json
     for the safety-relevant keys the carry-forward finding named.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from scorched.risk_gates import DEFAULT_MAX_POSITION_PCT, check_position_cap
from scorched.trailing_stops import (
    compute_trailing_stop,
    initial_stop_price,
    update_trailing_stop,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


class TestConfigurableTrailingStop:
    """strategy.json's trailing_stop.{atr_multiplier,floor_pct} must actually
    change the computed stop when threaded through."""

    def test_custom_atr_multiplier_and_floor_used(self):
        # HWM=110, ATR=1.5, atr_multiplier=2.5 => ATR stop = 110 - 3.75 = 106.25
        # Fixed floor: entry=100, floor_pct=6.0 => 100 * 0.94 = 94
        # max(...) picks the ATR stop since it's the tighter (higher) one.
        result = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("110"),
            high_water_mark=Decimal("110"),
            atr=1.5,
            atr_multiplier=2.5,
            min_stop_pct=6.0,
        )
        assert result["trailing_stop_price"] == Decimal("106.2500")
        assert result["stop_type"] == "atr"

    def test_custom_floor_used_when_atr_stop_looser(self):
        # HWM=100, ATR=10, atr_multiplier=2.5 => ATR stop = 100 - 25 = 75
        # Fixed floor: entry=100, floor_pct=6.0 => 94
        # max(75, 94) => fixed floor wins.
        result = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("100"),
            high_water_mark=Decimal("100"),
            atr=10.0,
            atr_multiplier=2.5,
            min_stop_pct=6.0,
        )
        assert result["trailing_stop_price"] == Decimal("94.0000")
        assert result["stop_type"] == "fixed_pct"

    def test_update_trailing_stop_forwards_custom_config(self):
        """update_trailing_stop (the wrapper cron/intraday_monitor.py calls)
        must accept and forward atr_multiplier/min_stop_pct — previously it
        silently dropped them and always used the hardcoded 2.0/5.0."""
        state = {"high_water_mark": 110.0, "trailing_stop_price": None}
        result = update_trailing_stop(
            state,
            current_price=110.0,
            atr=1.5,
            entry_price=100.0,
            atr_multiplier=2.5,
            min_stop_pct=6.0,
        )
        assert result["trailing_stop_price"] == pytest.approx(106.25)

    def test_defaults_unchanged_atr_multiplier_2_0(self):
        """No config threaded (default call) must be byte-identical to the
        pre-Task-7 behavior — this is a config *surface*, not a behavior change."""
        result = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("110"),
            high_water_mark=Decimal("110"),
            atr=1.5,
        )
        assert result["trailing_stop_price"] == Decimal("107.0000")

    def test_defaults_unchanged_floor_pct_5_0(self):
        result = compute_trailing_stop(
            entry_price=Decimal("100"),
            current_price=Decimal("100"),
            high_water_mark=None,
            atr=None,
        )
        assert result["trailing_stop_price"] == Decimal("95.0000")


class TestInitialStopPrice:
    """New-position day-one stop (services/portfolio.py apply_buy,
    services/reconciliation.py) — previously hardcoded `price * Decimal("0.95")`."""

    def test_default_5_pct_floor(self):
        assert initial_stop_price(Decimal("100")) == Decimal("95.0000")

    def test_custom_floor_pct(self):
        assert initial_stop_price(Decimal("100"), min_stop_pct=6.0) == Decimal("94.0000")

    def test_matches_old_hardcoded_formula_at_default(self):
        """Regression guard: default output must equal the old
        `execution_price * Decimal("0.95")` behavior exactly."""
        price = Decimal("237.4321")
        old = (price * Decimal("0.95")).quantize(Decimal("0.0001"))
        assert initial_stop_price(price) == old

    def test_matches_old_rounding_mode_at_genuine_tie(self):
        """Regression guard for a real bug caught in review: the old inline
        formula `(execution_price * Decimal("0.95")).quantize(Decimal("0.0001"))`
        quantizes with the ambient context's rounding mode (ROUND_HALF_EVEN by
        default) — it never passed an explicit rounding mode. An earlier draft
        of `initial_stop_price` passed ROUND_HALF_UP explicitly, which is a
        real (if tiny) behavior change at exact tie values, violating the
        "no behavior change" constraint on this task.

        entry_price=100.003 => raw = 100.003 * 0.95 = 95.00285 exactly, a
        genuine tie at the 4th decimal place (preceding digit 8 is even):
          ROUND_HALF_EVEN (correct / old behavior) -> 95.0028
          ROUND_HALF_UP   (the bug)                -> 95.0029
        """
        price = Decimal("100.003")
        old = (price * Decimal("0.95")).quantize(Decimal("0.0001"))
        assert old == Decimal("95.0028"), "test fixture itself must reproduce the tie"
        result = initial_stop_price(price)
        assert result == old == Decimal("95.0028")
        assert result != Decimal("95.0029"), "would indicate ROUND_HALF_UP crept back in"


class TestStaleMaxPositionPctFallback:
    """The '33 -> 15' sizing fallback fix."""

    def test_default_constant_is_15_not_33(self):
        assert DEFAULT_MAX_POSITION_PCT == 15

    def test_check_position_cap_with_missing_key_uses_15(self):
        """Simulates a strategy.json missing 'concentration.max_position_pct':
        callers do `conc.get("max_position_pct", DEFAULT_MAX_POSITION_PCT)`.
        A 20% position must now be rejected (>15%) where the old stale
        33% fallback would have passed it."""
        conc = {}  # key missing entirely
        cap = Decimal(str(conc.get("max_position_pct", DEFAULT_MAX_POSITION_PCT)))
        result = check_position_cap(
            existing_market_value=Decimal("0"),
            buy_notional=Decimal("20000"),
            total_portfolio_value=Decimal("100000"),
            max_position_pct=cap,
        )
        assert cap == Decimal("15")
        assert result.passed is False
        assert result.cap_pct == 15.0

    @pytest.mark.parametrize(
        "rel_path",
        [
            "src/scorched/services/recommender.py",
            "src/scorched/services/trade_execution.py",
            "src/scorched/services/strategy.py",
        ],
    )
    def test_no_stale_33_fallback_literal_remains(self, rel_path):
        """Regression guard: none of the fixed `.get("max_position_pct", 33)`
        call sites may hardcode the stale 33 default any more."""
        content = (_REPO_ROOT / rel_path).read_text()
        assert not re.search(r"max_position_pct['\"]?\s*,\s*33\b", content), (
            f"stale '33' max_position_pct fallback still present in {rel_path}"
        )

    def test_no_stale_33_default_in_claude_client(self):
        """Regression guard for the round-1 review fix: claude_client.py's
        `call_decision(..., max_position_pct: int = 33, ...)` keyword default
        was missed in the first pass. Must use DEFAULT_MAX_POSITION_PCT, not
        a bare 33 literal."""
        content = (_REPO_ROOT / "src/scorched/services/claude_client.py").read_text()
        assert not re.search(r"max_position_pct\s*:\s*int\s*=\s*33\b", content), (
            "stale 'max_position_pct: int = 33' default still present in claude_client.py"
        )
        assert "DEFAULT_MAX_POSITION_PCT" in content, (
            "claude_client.py should import/use risk_gates.DEFAULT_MAX_POSITION_PCT "
            "for the max_position_pct default"
        )

    def test_no_stale_33_default_preset_in_dashboard(self):
        """Regression guard for the round-1 review fix: strategy.html's
        max_position_pct numeric-preset field had `defaultPreset: 33` — the
        collectStrategy() fallback path silently PUTs this back into live
        strategy.json when the DOM element isn't found. Must not regress to
        33 (33 may still legitimately appear in the `presets:` options list
        itself, so this only checks the `defaultPreset` token)."""
        content = (_REPO_ROOT / "src/scorched/static/strategy.html").read_text()
        max_pos_row = next(
            (line for line in content.splitlines() if '"max_position_pct"' in line),
            None,
        )
        assert max_pos_row is not None, "max_position_pct field row not found in strategy.html"
        assert not re.search(r"defaultPreset\s*:\s*33\b", max_pos_row), (
            f"stale 'defaultPreset: 33' still present on max_position_pct row: {max_pos_row}"
        )


class TestDefaultJsonMirrorsLiveStrategy:
    """Carry-forward finding: DEFAULT_JSON (used when strategy.json is
    missing/corrupt) must mirror the live experiment config for the
    safety-relevant keys that previously reverted silently on fallback."""

    _KEYS_TO_CHECK = [
        ("entry_style", None),
        ("sell_discipline", None),
        ("partial_sell", None),
    ]

    def test_default_json_matches_live_for_named_keys(self):
        from scorched.services.strategy import DEFAULT_JSON

        live = json.loads((_REPO_ROOT / "strategy.json").read_text())

        assert DEFAULT_JSON["objective"] == live["objective"]
        assert DEFAULT_JSON["entry_style"] == live["entry_style"]
        assert DEFAULT_JSON["sell_discipline"] == live["sell_discipline"]
        assert DEFAULT_JSON["partial_sell"] == live["partial_sell"]
        assert (
            DEFAULT_JSON["concentration"]["max_position_pct"]
            == live["concentration"]["max_position_pct"]
        )

    def test_default_json_has_trailing_stop_section_matching_live(self):
        from scorched.services.strategy import DEFAULT_JSON

        live = json.loads((_REPO_ROOT / "strategy.json").read_text())

        assert "trailing_stop" in DEFAULT_JSON
        assert "trailing_stop" in live
        assert DEFAULT_JSON["trailing_stop"] == live["trailing_stop"]


class TestStrategyJsonTrailingStopSection:
    def test_live_strategy_json_has_trailing_stop_section(self):
        live = json.loads((_REPO_ROOT / "strategy.json").read_text())
        assert live["trailing_stop"]["atr_multiplier"] == 2.0
        assert live["trailing_stop"]["floor_pct"] == 5.0
