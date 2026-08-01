"""Tests for the exposure-management advisory target (Task 8).

Ceiling (overinvested) stays enforced by existing position/cash/holdings/
sector gates — assess_exposure is advisory-but-loud telemetry: it feeds a
context section + hard rule #10 that Claude must answer to, plus a
gate_decisions row every session for audit.
"""
import logging

from scorched.services.recommender import assess_exposure
from scorched.services.research import _format_exposure_status

CFG = {
    "target_min_invested_pct": 60,
    "target_max_invested_pct": 90,
    "regime_condition": "spy_above_20dma_and_no_drawdown_gate",
}


def test_underinvested_when_below_floor_and_regime_healthy():
    result = assess_exposure(12.0, spy_above_20dma=True, drawdown_gate_active=False, cfg=CFG)
    assert result["status"] == "underinvested"
    assert result["invested_pct"] == 12.0
    assert result["target_min"] == 60
    assert result["target_max"] == 90


def test_defensive_ok_when_below_floor_but_spy_below_20dma():
    result = assess_exposure(12.0, spy_above_20dma=False, drawdown_gate_active=False, cfg=CFG)
    assert result["status"] == "defensive_ok"


def test_defensive_ok_when_below_floor_but_drawdown_gate_active():
    # SPY healthy but drawdown gate active — still not a call to force buys.
    result = assess_exposure(12.0, spy_above_20dma=True, drawdown_gate_active=True, cfg=CFG)
    assert result["status"] == "defensive_ok"


def test_in_range_within_target_band():
    result = assess_exposure(70.0, spy_above_20dma=True, drawdown_gate_active=False, cfg=CFG)
    assert result["status"] == "in_range"


def test_overinvested_above_ceiling():
    result = assess_exposure(95.0, spy_above_20dma=True, drawdown_gate_active=False, cfg=CFG)
    assert result["status"] == "overinvested"


def test_overinvested_regardless_of_regime():
    # Ceiling is not regime-conditioned — over the cap is over the cap.
    result = assess_exposure(95.0, spy_above_20dma=False, drawdown_gate_active=True, cfg=CFG)
    assert result["status"] == "overinvested"


def test_boundary_at_target_min_is_in_range():
    result = assess_exposure(60.0, spy_above_20dma=True, drawdown_gate_active=False, cfg=CFG)
    assert result["status"] == "in_range"


def test_boundary_at_target_max_is_in_range():
    result = assess_exposure(90.0, spy_above_20dma=True, drawdown_gate_active=False, cfg=CFG)
    assert result["status"] == "in_range"


# --- regime_condition validation: honest handling of an unsupported string ---
# There is no config DSL — assess_exposure hardcodes the one supported
# condition. Editing strategy.json's regime_condition to anything else must
# not silently do nothing; it must warn and fall back to the hardcoded logic.


def test_unsupported_regime_condition_warns_and_falls_back(caplog):
    cfg = {**CFG, "regime_condition": "vix_below_20"}
    with caplog.at_level(logging.WARNING):
        result = assess_exposure(12.0, spy_above_20dma=True, drawdown_gate_active=False, cfg=cfg)
    # Falls back to the hardcoded spy_above_20dma_and_no_drawdown_gate logic —
    # behavior is unchanged, only the warning is new.
    assert result["status"] == "underinvested"
    assert any(
        "unsupported regime_condition" in rec.message and "vix_below_20" in rec.message
        for rec in caplog.records
    )


def test_supported_regime_condition_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING):
        assess_exposure(12.0, spy_above_20dma=True, drawdown_gate_active=False, cfg=CFG)
    assert not any("unsupported regime_condition" in rec.message for rec in caplog.records)


def test_missing_regime_condition_does_not_warn(caplog):
    # Absent key (e.g. an older strategy.json) is not treated as "unsupported"
    # — it just means the caller hasn't set it, no warning needed.
    cfg = {k: v for k, v in CFG.items() if k != "regime_condition"}
    with caplog.at_level(logging.WARNING):
        result = assess_exposure(12.0, spy_above_20dma=True, drawdown_gate_active=False, cfg=cfg)
    assert result["status"] == "underinvested"
    assert not any("unsupported regime_condition" in rec.message for rec in caplog.records)


# --- _format_exposure_status: rendered prompt text ---


def test_format_underinvested_mentions_hard_rule_and_percentages():
    status = assess_exposure(11.9, spy_above_20dma=True, drawdown_gate_active=False, cfg=CFG)
    lines = _format_exposure_status(status)
    text = "\n".join(lines)
    assert "hard rule #10" in text
    assert "11.9%" in text
    assert "60" in text and "90" in text
    assert "UNDERINVESTED" in text


def test_format_defensive_ok_does_not_demand_buys():
    status = assess_exposure(11.9, spy_above_20dma=False, drawdown_gate_active=False, cfg=CFG)
    lines = _format_exposure_status(status)
    text = "\n".join(lines)
    assert "DEFENSIVE_OK" in text
    # Must not tell Claude hard rule #10 *applies* — it only requires closing
    # the gap when the regime is healthy; low exposure here is appropriate,
    # not a shortfall to fix. Explicitly says it does NOT apply.
    assert "hard rule #10 applies" not in text
    assert "does not apply" in text


def test_format_returns_empty_list_when_no_status():
    assert _format_exposure_status(None) == []
    assert _format_exposure_status({}) == []
