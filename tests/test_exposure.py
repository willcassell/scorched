"""Tests for the exposure-management advisory target (Task 8).

Ceiling (overinvested) stays enforced by existing position/cash/holdings/
sector gates — assess_exposure is advisory-but-loud telemetry: it feeds a
context section + hard rule #10 that Claude must answer to, plus a
gate_decisions row every session for audit.
"""
from scorched.services.recommender import assess_exposure

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
