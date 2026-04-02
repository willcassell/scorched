"""Tests for intraday endpoint — hard stop bypass and normal Claude path."""
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from scorched.main import app
from scorched.database import get_db
from scorched.api.deps import require_owner_pin


@pytest.fixture
def _override_db(db_session):
    """Override the get_db dependency with our test session."""
    async def _get_db_override():
        yield db_session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[require_owner_pin] = lambda: None
    yield
    app.dependency_overrides.clear()


def _make_trigger(symbol="TEST", entry_price="100.00", current_price="93.00",
                  shares="10", trigger_reasons=None):
    return {
        "symbol": symbol,
        "trigger_reasons": trigger_reasons or ["position_drop_from_entry"],
        "current_price": current_price,
        "entry_price": entry_price,
        "today_open": "99.00",
        "today_high": "99.50",
        "today_low": "92.50",
        "days_held": 3,
        "shares": shares,
        "original_reasoning": "Test thesis",
    }


def _make_request(triggers, spy_change_pct=0.0, vix_current=20.0):
    return {
        "triggers": triggers,
        "market_context": {
            "spy_change_pct": spy_change_pct,
            "vix_current": vix_current,
        },
    }


@pytest.mark.asyncio
async def test_hard_stop_triggers_sell_without_claude(db_session, _override_db):
    """Position down 6% should trigger hard stop — Claude NOT called, sell executed."""
    # entry=100, current=94 => down 6%
    trigger = _make_trigger(entry_price="100.00", current_price="94.00")
    body = _make_request([trigger])

    mock_broker = AsyncMock()
    mock_broker.submit_sell.return_value = {
        "status": "filled",
        "trade_id": 99,
        "filled_avg_price": Decimal("94.00"),
        "realized_gain": Decimal("-6.00"),
    }

    with patch("scorched.api.intraday.get_broker", return_value=mock_broker), \
         patch("scorched.api.intraday.call_intraday_exit") as mock_claude, \
         patch("scorched.api.intraday.record_usage", new_callable=AsyncMock), \
         patch("scorched.api.intraday._load_hard_stop_pct", return_value=5.0):

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/v1/intraday/evaluate", json=body)

        assert resp.status_code == 200
        data = resp.json()
        decisions = data["decisions"]
        assert len(decisions) == 1
        d = decisions[0]
        assert d["symbol"] == "TEST"
        assert d["action"] == "exit_full"
        assert "Hard stop triggered" in d["reasoning"]
        assert "6.0%" in d["reasoning"]
        assert d["trade_result"] is not None
        assert d["trade_result"]["shares"] == 10.0

        # Claude must NOT have been called
        mock_claude.assert_not_called()
        # Broker sell must have been called
        mock_broker.submit_sell.assert_called_once()


@pytest.mark.asyncio
async def test_normal_trigger_goes_through_claude(db_session, _override_db):
    """Position down 3% should go through normal Claude evaluation path."""
    # entry=100, current=97 => down 3%
    trigger = _make_trigger(entry_price="100.00", current_price="97.00")
    body = _make_request([trigger])

    mock_broker = AsyncMock()
    mock_broker.submit_sell.return_value = {
        "status": "filled",
        "trade_id": 100,
        "filled_avg_price": Decimal("97.00"),
        "realized_gain": Decimal("-3.00"),
    }

    # Mock Claude response
    mock_response = MagicMock()
    mock_response.model = "claude-sonnet-4-6"
    mock_response.usage.input_tokens = 100
    mock_response.usage.output_tokens = 50
    raw_text = json.dumps({"action": "exit_full", "reasoning": "Momentum broken", "partial_pct": None})

    with patch("scorched.api.intraday.get_broker", return_value=mock_broker), \
         patch("scorched.api.intraday.call_intraday_exit", return_value=(mock_response, raw_text)) as mock_claude, \
         patch("scorched.api.intraday.record_usage", new_callable=AsyncMock), \
         patch("scorched.api.intraday._load_hard_stop_pct", return_value=5.0):

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/v1/intraday/evaluate", json=body)

        assert resp.status_code == 200
        data = resp.json()
        decisions = data["decisions"]
        assert len(decisions) == 1
        d = decisions[0]
        assert d["symbol"] == "TEST"
        assert d["action"] == "exit_full"
        assert "Momentum broken" in d["reasoning"]

        # Claude MUST have been called
        mock_claude.assert_called_once()


@pytest.mark.asyncio
async def test_hard_stop_exactly_at_threshold(db_session, _override_db):
    """Position down exactly 5% should trigger hard stop (>= threshold)."""
    # entry=100, current=95 => down exactly 5%
    trigger = _make_trigger(entry_price="100.00", current_price="95.00")
    body = _make_request([trigger])

    mock_broker = AsyncMock()
    mock_broker.submit_sell.return_value = {
        "status": "filled",
        "trade_id": 101,
        "filled_avg_price": Decimal("95.00"),
        "realized_gain": Decimal("-5.00"),
    }

    with patch("scorched.api.intraday.get_broker", return_value=mock_broker), \
         patch("scorched.api.intraday.call_intraday_exit") as mock_claude, \
         patch("scorched.api.intraday.record_usage", new_callable=AsyncMock), \
         patch("scorched.api.intraday._load_hard_stop_pct", return_value=5.0):

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/v1/intraday/evaluate", json=body)

        assert resp.status_code == 200
        d = resp.json()["decisions"][0]
        assert d["action"] == "exit_full"
        assert "Hard stop triggered" in d["reasoning"]
        mock_claude.assert_not_called()


@pytest.mark.asyncio
async def test_hard_stop_sell_failure_logged(db_session, _override_db):
    """If broker sell fails on hard stop, decision should note the failure."""
    trigger = _make_trigger(entry_price="100.00", current_price="93.00")
    body = _make_request([trigger])

    mock_broker = AsyncMock()
    mock_broker.submit_sell.side_effect = Exception("Broker down")

    with patch("scorched.api.intraday.get_broker", return_value=mock_broker), \
         patch("scorched.api.intraday.call_intraday_exit") as mock_claude, \
         patch("scorched.api.intraday.record_usage", new_callable=AsyncMock), \
         patch("scorched.api.intraday._load_hard_stop_pct", return_value=5.0):

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/v1/intraday/evaluate", json=body)

        assert resp.status_code == 200
        d = resp.json()["decisions"][0]
        assert "SELL FAILED" in d["reasoning"]
        assert d["action"] == "hold"
        mock_claude.assert_not_called()
