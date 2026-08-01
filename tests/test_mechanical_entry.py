"""Tests for Task 10: code-enforced mechanical entry gate.

Motivation (backtest evidence 6/18): LLM-originated entries lose to a
mechanical breakout rule. Every BUY must pass mechanical minimums IN CODE
from cached research data; Claude's role narrows to selecting/vetoing among
qualifying names, not exception-making.

Field mapping (there is no field literally named momentum_5d_pct / rel_volume
/ above_20dma anywhere in the codebase — those are the *logical* criteria
named in analyst_guidance.md hard rule #12; the real fields backing them are):

  - momentum    -> price_data[symbol]["week_change_pct"]
    (the true 5-trading-day return field already computed in
    _fetch_price_data_sync / research.py; rendered as "1wk" in
    build_research_context).
  - rel_volume  -> technicals[symbol]["volume"]["relative_volume"]
    (calc_volume_profile's latest/avg-20d ratio in technicals.py). The 9:45
    AM partial-bar bug that produced rel=0.0x on nearly every symbol was
    fixed in commit 997c2af (strips today's partial bar before computing the
    20d average) — verified clean against the live 2026-07-31 Phase-0 cache
    (78/78 symbols had a real, non-zero relative_volume in a plausible
    0.52x-3.42x range, no missing values). No systematic gap found, so this
    criterion gets the same fail-closed-by-default treatment as the other
    two — no special-cased warn+pass.
  - above_20dma -> price_data[symbol]["current_price"] compared against
    technicals[symbol]["bollinger"]["middle"] (calc_bollinger_bands' 20
    -period SMA, reused rather than adding a redundant MA calculation).
"""
from __future__ import annotations

import logging
from pathlib import Path

from scorched.services.recommender import check_mechanical_entry

CFG = {
    "enabled": True,
    "min_momentum_5d_pct": 0.0,
    "min_rel_volume": 1.0,
    "require_above_20dma": True,
}


def _price_data(week_change_pct=2.0, current_price=105.0):
    return {"AAPL": {"week_change_pct": week_change_pct, "current_price": current_price}}


def _technicals(relative_volume=1.4, ma20=100.0):
    return {
        "AAPL": {
            "volume": {"relative_volume": relative_volume},
            "bollinger": {"middle": ma20},
        }
    }


# --- Brief's five required cases ------------------------------------------


def test_all_criteria_pass_allows_buy():
    ok, reason = check_mechanical_entry(
        "AAPL", _price_data(2.0, 105.0), _technicals(1.4, 100.0), CFG
    )
    assert ok is True
    assert reason is None


def test_negative_momentum_blocks():
    ok, reason = check_mechanical_entry(
        "AAPL", _price_data(-1.0, 105.0), _technicals(1.4, 100.0), CFG
    )
    assert ok is False
    assert reason == "mechanical_momentum"


def test_low_relative_volume_blocks():
    ok, reason = check_mechanical_entry(
        "AAPL", _price_data(2.0, 105.0), _technicals(0.6, 100.0), CFG
    )
    assert ok is False
    assert reason == "mechanical_volume"


def test_below_20dma_blocks():
    # current_price (95.0) below the 20dma (100.0).
    ok, reason = check_mechanical_entry(
        "AAPL", _price_data(2.0, 95.0), _technicals(1.4, 100.0), CFG
    )
    assert ok is False
    assert reason == "mechanical_trend"


def test_missing_data_default_cfg_blocks_fail_closed():
    ok, reason = check_mechanical_entry("AAPL", {}, {}, CFG)
    assert ok is False
    assert reason == "mechanical_data_missing"


# --- Boundary semantics ------------------------------------------------


def test_momentum_exactly_at_floor_blocks():
    # Rule text is "5-day momentum > 0" — strictly greater, so exactly the
    # floor (0.0 with default config) does NOT qualify.
    ok, reason = check_mechanical_entry(
        "AAPL", _price_data(0.0, 105.0), _technicals(1.4, 100.0), CFG
    )
    assert ok is False
    assert reason == "mechanical_momentum"


def test_rel_volume_exactly_at_floor_allows():
    # Rule text is "relative volume >= 1.0" — non-strict, so exactly the
    # floor qualifies.
    ok, reason = check_mechanical_entry(
        "AAPL", _price_data(2.0, 105.0), _technicals(1.0, 100.0), CFG
    )
    assert ok is True
    assert reason is None


def test_price_exactly_at_20dma_blocks():
    # "Price above 20-day MA" — strictly greater, so exactly at the MA does
    # NOT qualify.
    ok, reason = check_mechanical_entry(
        "AAPL", _price_data(2.0, 100.0), _technicals(1.4, 100.0), CFG
    )
    assert ok is False
    assert reason == "mechanical_trend"


# --- Config behavior -----------------------------------------------------


def test_disabled_gate_always_passes():
    ok, reason = check_mechanical_entry(
        "AAPL", _price_data(-5.0, 50.0), _technicals(0.1, 100.0), {"enabled": False}
    )
    assert ok is True
    assert reason is None


def test_require_above_20dma_false_skips_trend_check():
    cfg = {**CFG, "require_above_20dma": False}
    ok, reason = check_mechanical_entry(
        "AAPL", _price_data(2.0, 50.0), _technicals(1.4, 100.0), cfg
    )
    assert ok is True
    assert reason is None


def test_missing_cfg_keys_use_defaults():
    # An empty (but enabled-by-default) cfg should still apply the documented
    # defaults: min_momentum_5d_pct=0.0, min_rel_volume=1.0,
    # require_above_20dma=True.
    ok, reason = check_mechanical_entry(
        "AAPL", _price_data(2.0, 105.0), _technicals(1.4, 100.0), {}
    )
    assert ok is True
    assert reason is None

    ok, reason = check_mechanical_entry(
        "AAPL", _price_data(-1.0, 105.0), _technicals(1.4, 100.0), {}
    )
    assert ok is False
    assert reason == "mechanical_momentum"


def test_malformed_numeric_config_degrades_to_defaults_and_logs_error(caplog):
    # A hand-edited strategy.json can put a non-numeric string / None into a
    # numeric field. That must not raise into the hot path (crashing Phase 1)
    # — it must degrade to the documented default (0.0 / 1.0) and log an
    # ERROR naming the key + exception type.
    cfg = {
        "enabled": True,
        "min_momentum_5d_pct": "not-a-number",
        "min_rel_volume": None,
        "require_above_20dma": True,
    }
    with caplog.at_level(logging.ERROR):
        ok, reason = check_mechanical_entry(
            "AAPL", _price_data(2.0, 105.0), _technicals(1.4, 100.0), cfg
        )
    # Defaults applied (0.0 / 1.0) -> momentum 2.0 > 0.0 and rel_vol 1.4 >=
    # 1.0 both clear, so the buy is allowed rather than the run crashing.
    assert ok is True
    assert reason is None
    error_messages = [rec.message for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert any("min_momentum_5d_pct" in m for m in error_messages)
    assert any("min_rel_volume" in m for m in error_messages)


def test_custom_thresholds_respected():
    cfg = {
        "enabled": True,
        "min_momentum_5d_pct": 1.0,
        "min_rel_volume": 1.5,
        "require_above_20dma": True,
    }
    # Momentum 0.5 is > 0.0 (old default) but not > 1.0 (custom floor).
    ok, reason = check_mechanical_entry(
        "AAPL", _price_data(0.5, 105.0), _technicals(2.0, 100.0), cfg
    )
    assert ok is False
    assert reason == "mechanical_momentum"


# --- Missing-data granularity (one criterion missing at a time) ----------


def test_missing_momentum_only_blocks_fail_closed():
    price_data = {"AAPL": {"current_price": 105.0}}  # week_change_pct absent
    ok, reason = check_mechanical_entry("AAPL", price_data, _technicals(1.4, 100.0), CFG)
    assert ok is False
    assert reason == "mechanical_data_missing"


def test_missing_relative_volume_only_blocks_fail_closed():
    technicals = {"AAPL": {"bollinger": {"middle": 100.0}}}  # no "volume" key
    ok, reason = check_mechanical_entry("AAPL", _price_data(2.0, 105.0), technicals, CFG)
    assert ok is False
    assert reason == "mechanical_data_missing"


def test_missing_20dma_only_blocks_fail_closed():
    technicals = {"AAPL": {"volume": {"relative_volume": 1.4}}}  # no "bollinger" key
    ok, reason = check_mechanical_entry("AAPL", _price_data(2.0, 105.0), technicals, CFG)
    assert ok is False
    assert reason == "mechanical_data_missing"


def test_fail_open_on_missing_true_passes_with_warning(caplog):
    cfg = {**CFG, "fail_open_on_missing": True}
    price_data = {"AAPL": {"current_price": 105.0}}  # week_change_pct missing
    with caplog.at_level(logging.WARNING):
        ok, reason = check_mechanical_entry(
            "AAPL", price_data, _technicals(1.4, 100.0), cfg
        )
    assert ok is True
    assert reason is None
    assert any("AAPL" in rec.message and "fail" in rec.message.lower() for rec in caplog.records)


def test_unknown_symbol_treated_as_missing_data():
    # Symbol not present in either dict at all.
    ok, reason = check_mechanical_entry("ZZZZ", _price_data(), _technicals(), CFG)
    assert ok is False
    assert reason == "mechanical_data_missing"


# --- Wiring regression guard (matches test_reentry_cooldown.py's pattern) --


def test_gate_is_wired_into_the_buy_chain_after_reentry_cooldown():
    """Regression guard: the unit tests above exercise check_mechanical_entry
    + record_gate_decision directly, which would still pass even if the call
    site in generate_recommendations() were deleted. Grep-assert it's
    actually wired, and last in the chain (per the brief: after
    reentry_cooldown)."""
    src = (
        Path(__file__).resolve().parent.parent
        / "src/scorched/services/recommender.py"
    ).read_text()
    assert 'gate="mechanical_entry"' in src
    assert "check_mechanical_entry(" in src
    assert src.index('gate="reentry_cooldown"') < src.index('gate="mechanical_entry"')
