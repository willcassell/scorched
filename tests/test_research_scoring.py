"""_score_symbol must reward up-moves and penalise (or zero) down-moves."""


def test_score_symbol_rewards_positive_week_change():
    from scorched.services.research import _score_symbol

    data_up = {"UP": {"week_change_pct": 5.0}}
    data_down = {"DN": {"week_change_pct": -5.0}}

    score_up = _score_symbol("UP", data_up, {}, None, None, None, None)
    score_down = _score_symbol("DN", data_down, {}, None, None, None, None)

    assert score_up > score_down, (
        f"Up move should outscore down move. up={score_up}, down={score_down}"
    )


def test_score_symbol_zero_change_scores_lower_than_positive():
    from scorched.services.research import _score_symbol

    data_up = {"UP": {"week_change_pct": 5.0}}
    data_flat = {"FLAT": {"week_change_pct": 0.0}}

    score_up = _score_symbol("UP", data_up, {}, None, None, None, None)
    score_flat = _score_symbol("FLAT", data_flat, {}, None, None, None, None)

    assert score_up > score_flat
