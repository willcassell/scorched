"""Mean-reversion screener must be gated by strategy.json's entry_style
config, not just suspended in the prompt.

Experiment B (2026-06-18) suspended mean-reversion entries by editing
analyst_guidance.md prose only — fetch_mean_reversion_screener kept running
every session, and its output kept flowing into Claude's context (Phase 0
cache + inline fallback) even though the prompt told Claude to ignore
mean-reversion setups. This test proves the screener call itself is gated
on strategy_json["entry_style"], in both the shared helper and its two call
sites (Phase 0 prefetch, recommender.py inline fallback).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_fetch_screeners_skips_mean_reversion_when_not_in_entry_style():
    from scorched.services.research import fetch_screeners

    strategy_json = {"entry_style": ["breakout"]}

    with patch(
        "scorched.services.research.fetch_momentum_screener",
        new=AsyncMock(return_value=["AAPL", "MSFT"]),
    ) as mock_momentum, patch(
        "scorched.services.research.fetch_mean_reversion_screener",
        new=AsyncMock(return_value=["XOM"]),
    ) as mock_mean_rev:
        screener_symbols, mean_reversion_symbols = await fetch_screeners(strategy_json)

    mock_momentum.assert_called_once()
    mock_mean_rev.assert_not_called()
    assert screener_symbols == ["AAPL", "MSFT"]
    assert mean_reversion_symbols == []


@pytest.mark.asyncio
async def test_fetch_screeners_runs_mean_reversion_when_in_entry_style():
    from scorched.services.research import fetch_screeners

    strategy_json = {"entry_style": ["breakout", "mean_reversion"]}

    with patch(
        "scorched.services.research.fetch_momentum_screener",
        new=AsyncMock(return_value=["AAPL", "MSFT"]),
    ) as mock_momentum, patch(
        "scorched.services.research.fetch_mean_reversion_screener",
        new=AsyncMock(return_value=["XOM"]),
    ) as mock_mean_rev:
        screener_symbols, mean_reversion_symbols = await fetch_screeners(strategy_json)

    mock_momentum.assert_called_once()
    mock_mean_rev.assert_called_once()
    assert screener_symbols == ["AAPL", "MSFT"]
    assert mean_reversion_symbols == ["XOM"]


@pytest.mark.asyncio
async def test_fetch_screeners_dedups_mean_reversion_against_momentum():
    """A symbol surfaced by both screeners should only appear once, in the
    momentum list (existing dedup behavior, preserved through the refactor).
    """
    from scorched.services.research import fetch_screeners

    strategy_json = {"entry_style": ["mean_reversion"]}

    with patch(
        "scorched.services.research.fetch_momentum_screener",
        new=AsyncMock(return_value=["AAPL"]),
    ), patch(
        "scorched.services.research.fetch_mean_reversion_screener",
        new=AsyncMock(return_value=["AAPL", "XOM"]),
    ):
        screener_symbols, mean_reversion_symbols = await fetch_screeners(strategy_json)

    assert screener_symbols == ["AAPL"]
    assert mean_reversion_symbols == ["XOM"]


def test_fetch_screeners_missing_entry_style_key_defaults_to_disabled():
    """strategy.json without an entry_style key must not crash and must not
    enable mean-reversion (fail toward the narrower, code-enforced behavior).
    """
    import asyncio
    from scorched.services.research import fetch_screeners

    async def _run():
        with patch(
            "scorched.services.research.fetch_momentum_screener",
            new=AsyncMock(return_value=["AAPL"]),
        ), patch(
            "scorched.services.research.fetch_mean_reversion_screener",
            new=AsyncMock(return_value=["XOM"]),
        ) as mock_mean_rev:
            screener_symbols, mean_reversion_symbols = await fetch_screeners({})
        mock_mean_rev.assert_not_called()
        assert mean_reversion_symbols == []

    asyncio.run(_run())


def test_research_context_builds_cleanly_with_mean_reversion_disabled():
    """build_research_context must not raise when mean_reversion_symbols is
    an empty list (the shape written to cache / passed inline when the
    screener is gated off) — proves the downstream context builder has no
    KeyError/None-handling gap for the disabled case.
    """
    from scorched.services.research import build_research_context

    portfolio = {"cash_balance": 10000.0, "total_value": 10000.0, "positions": []}
    price_data = {
        "AAPL": {
            "current_price": 150.0,
            "week_change_pct": 1.0,
            "month_change_pct": 3.0,
            "high_52w": 160.0,
            "low_52w": 120.0,
            "market_cap": None,
            "pe_ratio": None,
            "forward_pe": None,
            "eps_ttm": None,
            "short_ratio": None,
            "short_percent_float": None,
            "company_name": "Apple Inc.",
            "insider_buy_pct": None,
            "history_close": [145.0] * 250,
            "history_volume": [50_000_000] * 250,
            "history_high": [152.0] * 250,
            "history_low": [148.0] * 250,
        }
    }

    # Empty list (the value this change writes when mean_reversion is gated off)
    context = build_research_context(
        portfolio, price_data, {}, [],
        mean_reversion_symbols=[],
    )
    assert isinstance(context, str)
    assert len(context) > 0

    # Omitted entirely (default) must also work — cache.get(..., []) fallback path
    context_default = build_research_context(
        portfolio, price_data, {}, [],
    )
    assert isinstance(context_default, str)
    assert len(context_default) > 0


# ---------------------------------------------------------------------------
# gate_cached_mean_reversion — re-gates on cache READ, not just on Phase 0's
# write. Closes the stale-cache gap: a cache file written by an older
# container, or earlier the same day before a strategy.json edit, could still
# carry mean-reversion symbols even though the current entry_style disables
# it.
# ---------------------------------------------------------------------------

def test_gate_cached_mean_reversion_zeroes_out_stale_cache_when_disabled():
    from scorched.services.research import gate_cached_mean_reversion

    stale_cache = {"mean_reversion_symbols": ["XOM", "KO"]}
    strategy_json = {"entry_style": ["breakout"]}

    assert gate_cached_mean_reversion(stale_cache, strategy_json) == []


def test_gate_cached_mean_reversion_passes_through_when_enabled():
    from scorched.services.research import gate_cached_mean_reversion

    cache = {"mean_reversion_symbols": ["XOM", "KO"]}
    strategy_json = {"entry_style": ["breakout", "mean_reversion"]}

    assert gate_cached_mean_reversion(cache, strategy_json) == ["XOM", "KO"]


def test_gate_cached_mean_reversion_handles_missing_cache_key():
    from scorched.services.research import gate_cached_mean_reversion

    assert gate_cached_mean_reversion({}, {"entry_style": ["mean_reversion"]}) == []
    assert gate_cached_mean_reversion({}, {"entry_style": ["breakout"]}) == []


# ---------------------------------------------------------------------------
# Import-binding checks — a future edit that reverts either call site back to
# calling fetch_mean_reversion_screener() directly (bypassing the gate) would
# not be caught by the tests above, since those exercise fetch_screeners() in
# isolation. Assert both modules still wire through the shared helper.
# ---------------------------------------------------------------------------

def test_prefetch_uses_shared_fetch_screeners_helper():
    from scorched.api import prefetch
    from scorched.services import research

    assert prefetch.fetch_screeners is research.fetch_screeners


def test_recommender_uses_shared_fetch_screeners_helper():
    from scorched.services import recommender
    from scorched.services import research

    assert recommender.fetch_screeners is research.fetch_screeners
    assert recommender.gate_cached_mean_reversion is research.gate_cached_mean_reversion
