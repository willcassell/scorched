"""Tests for the experiment B-momentum-discipline factor-alignment gate."""
from scorched.services.recommender import check_factor_alignment

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


def test_missing_factor_data_passes():
    ok, reason = check_factor_alignment(-12.0, {}, CFG)
    assert ok is True and reason is None


def test_missing_candidate_momentum_passes():
    # Unknown own-momentum -> don't block on a data gap.
    ok, reason = check_factor_alignment(None, MOMENTUM_REGIME, CFG)
    assert ok is True and reason is None


def test_custom_floor_rejects_weak_positive():
    cfg = {"enabled": True, "min_factor_lead_pts": 3.0, "min_candidate_mom_pct": 5.0}
    ok, reason = check_factor_alignment(2.0, MOMENTUM_REGIME, cfg)
    assert ok is False
