"""Tests for the experiment B-momentum-discipline factor-alignment gate."""
import logging

from scorched.services.recommender import (
    check_factor_alignment,
    resolve_candidate_20d_momentum,
)

# A momentum-led regime: MTUM/QQQ lead SPY by well over 3 pts over 20d.
MOMENTUM_REGIME = {
    "SPY": {"20d": 2.0},
    "MTUM": {"20d": 8.0},
    "QQQ": {"20d": 7.0},
    "SPMO": {"20d": 9.0},
}
# A flat/broad regime: no momentum factor leads SPY by 3 pts.
BROAD_REGIME = {
    "SPY": {"20d": 4.0},
    "MTUM": {"20d": 5.0},
    "QQQ": {"20d": 4.5},
}
# IWM leads SPY by a lot, but IWM/RSP are NOT momentum factors the gate
# checks (small-cap/equal-weight breadth is a different regime — see
# analyst_guidance.md rule #9 discussion). MTUM/SPMO/QQQ do not lead here.
IWM_ONLY_REGIME = {
    "SPY": {"20d": 2.0},
    "IWM": {"20d": 9.0},
    "RSP": {"20d": 8.0},
    "MTUM": {"20d": 2.5},
    "QQQ": {"20d": 3.0},
}
CFG = {"enabled": True, "min_factor_lead_pts": 3.0, "min_candidate_mom_pct": 0.0}


def test_disabled_always_passes():
    ok, reason = check_factor_alignment(-20.0, MOMENTUM_REGIME, {"enabled": False})
    assert ok is True and reason is None


def test_momentum_regime_rejects_negative_own_momentum():
    # The energy/commodity falling-knife case: market is momentum-led but the
    # candidate is down 12% over 20 days -> reject.
    ok, reason = check_factor_alignment(-12.0, MOMENTUM_REGIME, CFG)
    assert ok is False
    assert "momentum regime" in reason


def test_momentum_regime_allows_positive_own_momentum():
    ok, reason = check_factor_alignment(6.5, MOMENTUM_REGIME, CFG)
    assert ok is True and reason is None


def test_no_momentum_regime_passes_even_if_candidate_down():
    # No factor leads SPY by >=3 pts -> gate inactive, a down name is allowed.
    ok, reason = check_factor_alignment(-12.0, BROAD_REGIME, CFG)
    assert ok is True and reason is None


def test_iwm_rsp_leadership_does_not_trigger_momentum_regime():
    # IWM/RSP are not among the checked momentum leaders (MTUM/SPMO/QQQ only).
    # A small-cap/equal-weight-led regime must NOT block a down candidate.
    ok, reason = check_factor_alignment(-12.0, IWM_ONLY_REGIME, CFG)
    assert ok is True and reason is None


def test_missing_factor_data_blocks_fail_closed():
    # Fail CLOSED (matching sector-gate posture) — a data gap must not
    # silently let a buy through the factor gate.
    ok, reason = check_factor_alignment(-12.0, {}, CFG)
    assert ok is False
    assert reason == "factor_data_missing"


def test_missing_spy_only_blocks_fail_closed():
    factor_returns = {"MTUM": {"20d": 8.0}, "QQQ": {"20d": 7.0}}
    ok, reason = check_factor_alignment(-12.0, factor_returns, CFG)
    assert ok is False
    assert reason == "factor_data_missing"


def test_missing_candidate_momentum_blocks_in_momentum_regime():
    # Unknown own-momentum inside an active momentum regime -> fail closed,
    # we can't verify the candidate isn't a falling knife.
    ok, reason = check_factor_alignment(None, MOMENTUM_REGIME, CFG)
    assert ok is False
    assert reason == "factor_data_missing"


def test_missing_candidate_momentum_passes_when_no_regime():
    # No momentum regime active -> gate is inactive regardless of candidate
    # data availability.
    ok, reason = check_factor_alignment(None, BROAD_REGIME, CFG)
    assert ok is True and reason is None


def test_custom_floor_rejects_weak_positive():
    cfg = {"enabled": True, "min_factor_lead_pts": 3.0, "min_candidate_mom_pct": 5.0}
    ok, reason = check_factor_alignment(2.0, MOMENTUM_REGIME, cfg)
    assert ok is False


# --- resolve_candidate_20d_momentum: true 20-trading-day return preference ---


def test_resolver_prefers_trailing_20d_return():
    price_row = {"trailing_20d_return_pct": 4.2, "month_change_pct": -1.0}
    result = resolve_candidate_20d_momentum("AAPL", price_row)
    assert result == 4.2


def test_resolver_falls_back_to_month_change_pct_with_warning(caplog):
    price_row = {"month_change_pct": 6.7}
    with caplog.at_level(logging.WARNING):
        result = resolve_candidate_20d_momentum("AAPL", price_row)
    assert result == 6.7
    assert any(
        "trailing_20d_return_pct" in rec.message and "AAPL" in rec.message
        for rec in caplog.records
    )


def test_resolver_returns_none_when_both_missing():
    result = resolve_candidate_20d_momentum("AAPL", {})
    assert result is None


def test_resolver_handles_none_price_row():
    result = resolve_candidate_20d_momentum("AAPL", None)
    assert result is None
