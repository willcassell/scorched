"""Tests: Twelvedata RSI and economic calendar are rendered in build_research_context."""

from scorched.services.research import build_research_context

# Minimal portfolio_dict / price_data shared across tests
_PORTFOLIO = {
    "cash_balance": 90000.0,
    "total_value": 100000.0,
    "positions": [],
}
_PRICE = {
    "AAPL": {
        "current_price": 200.0,
        "week_change_pct": 4.2,
        "month_change_pct": 6.0,
        "high_52w": 220.0,
        "low_52w": 160.0,
        "market_cap": None,
        "pe_ratio": None,
        "forward_pe": None,
        "eps_ttm": None,
        "short_ratio": None,
        "short_percent_float": None,
        "company_name": "Apple Inc.",
        "insider_buy_pct": None,
        "history_close": [190.0] * 250,
        "history_volume": [50_000_000] * 250,
        "history_high": [202.0] * 250,
        "history_low": [198.0] * 250,
    }
}


def _ctx(**kwargs) -> str:
    """Call build_research_context with defaults, override via kwargs."""
    defaults = dict(
        portfolio_dict=_PORTFOLIO,
        price_data=_PRICE,
        news_data={},
        current_symbols=[],
    )
    defaults.update(kwargs)
    return build_research_context(**defaults)


# ── Twelvedata RSI tests ────────────────────────────────────────────────────

def test_twelvedata_rsi_rendered_when_present():
    """RSI value from Twelvedata appears in context."""
    ctx = _ctx(twelvedata_rsi={"AAPL": {"rsi": 62.3, "signal": "neutral"}})
    assert "RSI(14)" in ctx
    assert "62.3" in ctx


def test_twelvedata_rsi_preferred_over_av_when_both_present():
    """Twelvedata RSI (full watchlist) takes priority over Alpha Vantage (screener-only)."""
    ctx = _ctx(
        twelvedata_rsi={"AAPL": {"rsi": 62.3, "signal": "neutral"}},
        av_technicals={"AAPL": {"rsi": 55.0, "signal": "neutral"}},
    )
    assert "62.3" in ctx
    # AV value should not appear since TD is preferred
    assert "55.0" not in ctx


def test_av_rsi_used_when_no_twelvedata():
    """Alpha Vantage RSI is rendered when Twelvedata is absent."""
    ctx = _ctx(av_technicals={"AAPL": {"rsi": 55.0, "signal": "neutral"}})
    assert "RSI(14)" in ctx
    assert "55.0" in ctx


def test_no_rsi_when_neither_source_present():
    """When neither RSI source has data for a symbol, RSI line is omitted."""
    ctx = _ctx()
    # RSI line should not appear for AAPL (no source provided)
    assert "RSI(14)" not in ctx


# ── Economic calendar tests ─────────────────────────────────────────────────

def test_economic_calendar_rendered_when_present():
    """Economic calendar block appears in context when non-empty."""
    econ = "## Upcoming Economic Releases (Next 7 Days)\n  2026-04-20 — CPI (Consumer Price Index) (in 2 days)"
    ctx = _ctx(economic_calendar_context=econ)
    assert "UPCOMING ECONOMIC RELEASES" in ctx
    assert "CPI" in ctx


def test_economic_calendar_omitted_when_empty_string():
    """Economic calendar section is omitted when context is empty string."""
    ctx = _ctx(economic_calendar_context="")
    assert "UPCOMING ECONOMIC RELEASES" not in ctx


def test_economic_calendar_omitted_when_none():
    """Economic calendar section is omitted when context is None (default)."""
    ctx = _ctx()
    assert "UPCOMING ECONOMIC RELEASES" not in ctx


def test_same_day_release_flag_in_calendar():
    """TODAY label appears in context for same-day releases (from build_economic_calendar_context)."""
    from scorched.services.economic_calendar import build_economic_calendar_context
    events = [{"name": "CPI (Consumer Price Index)", "date": "2026-04-18", "release_id": 10, "days_until": 0}]
    econ = build_economic_calendar_context(events)
    assert "TODAY" in econ
    ctx = _ctx(economic_calendar_context=econ)
    assert "TODAY" in ctx


# ── Portfolio risk (VaR/CVaR) tests ─────────────────────────────────────────

def test_portfolio_risk_omitted_when_none():
    """When portfolio_risk is None (default), the risk section is not rendered."""
    ctx = _ctx()
    assert "PORTFOLIO RISK" not in ctx


def test_portfolio_risk_omitted_when_empty_book():
    """An empty book (n_positions=0) suppresses the section — nothing to render."""
    risk = {
        "var_pct": 0.0,
        "cvar_pct": 0.0,
        "var_dollars": 0.0,
        "cvar_dollars": 0.0,
        "confidence": 0.95,
        "lookback_days": 0,
        "n_positions": 0,
        "portfolio_value": 100000.0,
    }
    ctx = _ctx(portfolio_risk=risk)
    assert "PORTFOLIO RISK" not in ctx


def test_portfolio_risk_rendered_when_populated():
    """Populated VaR/CVaR renders header, both rows, and basis line with correct formatting."""
    risk = {
        "var_pct": -0.024,    # -2.4%
        "cvar_pct": -0.038,   # -3.8%
        "var_dollars": 2400.0,
        "cvar_dollars": 3800.0,
        "confidence": 0.95,
        "lookback_days": 251,
        "n_positions": 5,
        "portfolio_value": 100000.0,
    }
    ctx = _ctx(portfolio_risk=risk)
    assert "PORTFOLIO RISK (1-day historical-sim, 95%)" in ctx
    # Both percentages reported as positive-magnitude losses
    assert "VaR:  2.4%" in ctx
    assert "CVaR: 3.8%" in ctx
    # Dollar magnitudes formatted with thousands separator
    assert "$2,400" in ctx
    assert "$3,800" in ctx
    # Basis line lists position count + lookback
    assert "5 positions" in ctx
    assert "251 days" in ctx
    # Tail-frequency phrasing — VaR exceeded ~5% of historical days at 95% confidence
    assert "5% of historical days" in ctx


def test_portfolio_risk_accepts_dataclass():
    """Dataclass instances (the live in-process shape) render the same as dicts."""
    from scorched.services.risk import HistoricalSimResult
    risk = HistoricalSimResult(
        var_pct=-0.020,
        cvar_pct=-0.030,
        var_dollars=2000.0,
        cvar_dollars=3000.0,
        confidence=0.99,
        lookback_days=250,
        n_positions=3,
        portfolio_value=100000.0,
    )
    ctx = _ctx(portfolio_risk=risk)
    assert "PORTFOLIO RISK (1-day historical-sim, 99%)" in ctx
    assert "VaR:  2.0%" in ctx
    assert "CVaR: 3.0%" in ctx
    assert "1% of historical days" in ctx
