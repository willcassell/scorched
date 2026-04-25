from scorched.services.guidance_lint import _check_stop_loss


def test_stop_loss_passes_when_aligned():
    strategy = {"intraday_monitor": {"hard_stop_pct": 8.0}}
    guidance = "4. **Stop loss at -8% from entry**: Any position down 8%..."
    finding = _check_stop_loss(strategy, guidance)
    assert finding.severity == "ok"


def test_stop_loss_errors_on_mismatch():
    strategy = {"intraday_monitor": {"hard_stop_pct": 5.0}}
    guidance = "4. **Stop loss at -8% from entry**: Any position down 8%..."
    finding = _check_stop_loss(strategy, guidance)
    assert finding.severity == "error"
    assert "5.0" in finding.message and "8" in finding.message


def test_stop_loss_errors_when_strategy_missing():
    strategy = {}
    guidance = "4. **Stop loss at -8% from entry**"
    finding = _check_stop_loss(strategy, guidance)
    assert finding.severity == "error"
