"""Tests for Task 7: Alpaca detailed news wired into Phase 0."""
import asyncio
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_alpaca_articles(symbol: str):
    """Return minimal Alpaca-shaped news articles for a symbol."""
    return [
        {
            "headline": f"{symbol} beats Q4 earnings estimates",
            "summary": f"{symbol} reported strong quarterly results, exceeding analyst consensus.",
            "source": "Benzinga",
            "created_at": "2026-04-18T12:00:00Z",
            "symbols": [symbol],
        },
        {
            "headline": f"{symbol} announces new product line",
            "summary": "",
            "source": "Reuters",
            "created_at": "2026-04-17T09:30:00Z",
            "symbols": [symbol],
        },
    ]


# ---------------------------------------------------------------------------
# fetch_news_sync shape test (mocks the Alpaca NewsClient)
# ---------------------------------------------------------------------------

def test_fetch_news_sync_returns_expected_shape():
    """fetch_news_sync returns {symbol: [{"headline", "summary", ...}]} shape."""
    from scorched.services.alpaca_data import fetch_news_sync

    mock_article = MagicMock()
    mock_article.get = lambda k, default="": {
        "headline": "AAPL beats estimates",
        "summary": "Strong quarter driven by services.",
        "source": "Benzinga",
        "created_at": "2026-04-18T12:00:00Z",
        "symbols": ["AAPL"],
    }.get(k, default)

    mock_news_set = MagicMock()
    mock_news_set.dict.return_value = {"news": [mock_article.get.__self__._mock_return_value if False else {
        "headline": "AAPL beats estimates",
        "summary": "Strong quarter driven by services.",
        "source": "Benzinga",
        "created_at": "2026-04-18T12:00:00Z",
        "symbols": ["AAPL"],
    }]}

    mock_client = MagicMock()
    mock_client.get_news.return_value = mock_news_set

    with patch("scorched.services.alpaca_data._get_news_client", return_value=mock_client), \
         patch("time.sleep"):
        result = fetch_news_sync(["AAPL"], limit_per_symbol=5)

    assert "AAPL" in result
    articles = result["AAPL"]
    assert len(articles) == 1
    assert articles[0]["headline"] == "AAPL beats estimates"
    assert articles[0]["summary"] == "Strong quarter driven by services."
    assert "source" in articles[0]


def test_fetch_news_sync_skips_articles_without_headline():
    """Articles with empty headline are filtered out."""
    from scorched.services.alpaca_data import fetch_news_sync

    mock_news_set = MagicMock()
    mock_news_set.dict.return_value = {"news": [
        {"headline": "", "summary": "orphan summary", "source": "X", "created_at": "", "symbols": ["MSFT"]},
        {"headline": "MSFT raises guidance", "summary": "Positive outlook.", "source": "Y", "created_at": "", "symbols": ["MSFT"]},
    ]}

    mock_client = MagicMock()
    mock_client.get_news.return_value = mock_news_set

    with patch("scorched.services.alpaca_data._get_news_client", return_value=mock_client), \
         patch("time.sleep"):
        result = fetch_news_sync(["MSFT"])

    articles = result["MSFT"]
    assert len(articles) == 1
    assert articles[0]["headline"] == "MSFT raises guidance"


# ---------------------------------------------------------------------------
# build_research_context: summary rendering
# ---------------------------------------------------------------------------

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
    from scorched.services.research import build_research_context
    defaults = dict(
        portfolio_dict=_PORTFOLIO,
        price_data=_PRICE,
        news_data={},
        current_symbols=[],
    )
    defaults.update(kwargs)
    return build_research_context(**defaults)


def test_build_research_context_renders_summary_when_present():
    """Headline + summary both appear in context when summary is non-empty."""
    detailed = {
        "AAPL": [{"title": "AAPL beats Q4", "description": "Strong quarterly results exceeded estimates."}]
    }
    ctx = _ctx(detailed_news=detailed)
    assert "AAPL beats Q4" in ctx
    assert "Strong quarterly results exceeded estimates." in ctx


def test_build_research_context_title_only_when_no_summary():
    """Only headline appears when description/summary is absent."""
    detailed = {
        "AAPL": [{"title": "AAPL announces buyback", "description": ""}]
    }
    ctx = _ctx(detailed_news=detailed)
    assert "AAPL announces buyback" in ctx
    # Colon separator only present when description follows
    # The line should not have trailing ":"
    for line in ctx.splitlines():
        if "AAPL announces buyback" in line:
            assert not line.rstrip().endswith(":"), f"Unexpected trailing colon in: {line!r}"


def test_build_research_context_falls_back_to_yfinance_when_no_detailed_news():
    """yfinance headlines used when detailed_news is absent/empty."""
    ctx = _ctx(
        detailed_news=None,
        news_data={"AAPL": ["AAPL hits all-time high on strong demand"]},
    )
    assert "AAPL hits all-time high on strong demand" in ctx


def test_build_research_context_detailed_news_preferred_over_yfinance():
    """Detailed news takes priority over yfinance headlines for the same symbol."""
    detailed = {
        "AAPL": [{"title": "AAPL detailed headline", "description": "Rich summary here."}]
    }
    ctx = _ctx(
        detailed_news=detailed,
        news_data={"AAPL": ["AAPL yfinance headline"]},
    )
    assert "AAPL detailed headline" in ctx
    assert "Rich summary here." in ctx
    # yfinance headline should not appear for this symbol
    assert "AAPL yfinance headline" not in ctx


# ---------------------------------------------------------------------------
# _score_symbol: detailed news boosts score
# ---------------------------------------------------------------------------

def test_score_symbol_boosts_when_detailed_news_present():
    """_score_symbol awards a higher score when detailed_news has articles."""
    from scorched.services.research import _score_symbol

    price_data = {"AAPL": {"week_change_pct": 0.0}}
    detailed_with_news = {"AAPL": [{"title": "Big news", "description": ""}]}
    detailed_empty = {}

    score_with_news = _score_symbol("AAPL", price_data, {}, detailed_with_news, None, None, None)
    score_without = _score_symbol("AAPL", price_data, {}, detailed_empty, None, None, None)

    assert score_with_news > score_without, (
        f"Detailed news should boost score. with={score_with_news}, without={score_without}"
    )


def test_score_symbol_detailed_news_beats_yfinance_headlines():
    """Detailed news scores higher than yfinance-only headlines (existing behaviour)."""
    from scorched.services.research import _score_symbol

    price_data = {"AAPL": {"week_change_pct": 0.0}}
    detailed = {"AAPL": [{"title": "Detailed article", "description": "Summary."}]}
    yf_news = {"AAPL": ["yf headline"]}

    score_detailed = _score_symbol("AAPL", price_data, yf_news, detailed, None, None, None)
    score_yf_only = _score_symbol("AAPL", price_data, yf_news, {}, None, None, None)

    assert score_detailed > score_yf_only


# ---------------------------------------------------------------------------
# Legacy polygon_news kwarg still accepted by build_research_context
# ---------------------------------------------------------------------------

def test_build_research_context_accepts_polygon_news_kwarg():
    """Legacy polygon_news kwarg still works (backward compat)."""
    poly_news = {
        "AAPL": [{"title": "Legacy polygon headline", "description": "Legacy summary."}]
    }
    ctx = _ctx(polygon_news=poly_news)
    assert "Legacy polygon headline" in ctx
    assert "Legacy summary." in ctx
