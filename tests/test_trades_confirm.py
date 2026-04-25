"""Tests for the hardened /trades/confirm endpoint (audit C1–C3, I1, I3)."""
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from scorched.main import app
from scorched.database import get_db
from scorched.api.deps import require_owner_pin
from scorched.models import TradeRecommendation, RecommendationSession


@pytest.fixture
def _override_db(db_session):
    """Override the get_db dependency with our test session."""
    async def _get_db_override():
        yield db_session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[require_owner_pin] = lambda: None
    yield
    app.dependency_overrides.clear()


async def _make_rec(db_session, symbol="AAPL", action="buy", quantity=Decimal("10"),
                    suggested_price=Decimal("150.00"), confidence="high"):
    """Create a RecommendationSession + TradeRecommendation for testing."""
    session = RecommendationSession(session_date=date.today())
    db_session.add(session)
    await db_session.flush()
    rec = TradeRecommendation(
        session_id=session.id,
        symbol=symbol, action=action, quantity=quantity,
        suggested_price=suggested_price, confidence=confidence,
        reasoning="test", key_risks="", status="pending",
    )
    db_session.add(rec)
    await db_session.commit()
    return rec


# ---------------------------------------------------------------------------
# Buy-path tests (C1 / original audit tests)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_uses_stored_rec_quantity_not_client_qty(db_session, _override_db):
    """Audit C1: client-supplied shares must not override stored rec quantity."""
    rec = await _make_rec(db_session)

    fake_broker = AsyncMock()
    fake_broker.submit_buy.return_value = {
        "status": "submitted",
        "filled_qty": Decimal("10"),
        "filled_avg_price": Decimal("150"),
    }

    gate_result = MagicMock()
    gate_result.passed = True

    # Buy path uses validate_and_submit_trade — patch at the service module level.
    snapshot_data = {"AAPL": {"current_price": 150.5, "prev_close": 149.0}}
    with patch("scorched.services.alpaca_data.fetch_snapshots_sync", return_value=snapshot_data), \
         patch("scorched.services.trade_execution.get_broker", return_value=fake_broker), \
         patch("scorched.services.trade_execution.run_all_buy_gates", return_value=gate_result):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/trades/confirm",
                json={"recommendation_id": rec.id, "shares": "9999", "execution_price": "1.00"},
            )

    assert r.status_code == 200
    fake_broker.submit_buy.assert_called_once()
    kwargs = fake_broker.submit_buy.call_args.kwargs
    assert kwargs["qty"] == Decimal("10")  # from stored rec, not 9999


@pytest.mark.asyncio
async def test_confirm_rejects_when_gates_fail(db_session, _override_db):
    """If cash floor / position cap / etc fail at confirm time, broker is NOT called."""
    rec = await _make_rec(db_session)

    fake_broker = AsyncMock()

    gate_result = MagicMock()
    gate_result.passed = False
    gate_result.reason = "cash floor would breach"

    snapshot_data = {"AAPL": {"current_price": 150.5, "prev_close": 149.0}}
    with patch("scorched.services.alpaca_data.fetch_snapshots_sync", return_value=snapshot_data), \
         patch("scorched.services.trade_execution.get_broker", return_value=fake_broker), \
         patch("scorched.services.trade_execution.run_all_buy_gates", return_value=gate_result):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/trades/confirm",
                json={"recommendation_id": rec.id},
            )

    assert r.status_code == 422
    assert "cash floor" in r.text.lower()
    fake_broker.submit_buy.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_rejects_when_live_price_drifts_beyond_tolerance(db_session, _override_db):
    """Stored price was $150; live $200 -> 33% drift > 5% tolerance -> reject (buys only)."""
    rec = await _make_rec(db_session)

    fake_broker = AsyncMock()

    # $200 live vs $150 stored = 33.3% drift, well above 5% default
    snapshot_data = {"AAPL": {"current_price": 200.0, "prev_close": 149.0}}
    with patch("scorched.services.alpaca_data.fetch_snapshots_sync", return_value=snapshot_data), \
         patch("scorched.services.trade_execution.get_broker", return_value=fake_broker):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/trades/confirm",
                json={"recommendation_id": rec.id},
            )

    assert r.status_code == 422
    assert "drift" in r.text.lower() or "tolerance" in r.text.lower()
    fake_broker.submit_buy.assert_not_called()


# ---------------------------------------------------------------------------
# Sell-path tests (C3 / I3)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_sell_skips_buy_gates(db_session, _override_db):
    """Sells must NOT run run_all_buy_gates — they always pass through."""
    rec = await _make_rec(db_session, action="sell")

    fake_broker = AsyncMock()
    fake_broker.submit_sell.return_value = {
        "status": "submitted",
        "filled_qty": Decimal("10"),
        "filled_avg_price": Decimal("150"),
    }

    with patch("scorched.services.trade_execution._fetch_live_price_single", return_value=Decimal("150.0")), \
         patch("scorched.services.trade_execution.get_broker", return_value=fake_broker), \
         patch("scorched.services.trade_execution.run_all_buy_gates") as mock_gates:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/api/v1/trades/confirm", json={"recommendation_id": rec.id})

    assert r.status_code == 200
    fake_broker.submit_sell.assert_called_once()
    mock_gates.assert_not_called()  # sells skip the buy gates


@pytest.mark.asyncio
async def test_confirm_sell_passes_despite_large_drift(db_session, _override_db):
    """A 20% gap-down on a sell must NOT be rejected (audit C3 / LRCX pattern)."""
    rec = await _make_rec(db_session, action="sell", suggested_price=Decimal("150.00"))

    fake_broker = AsyncMock()
    fake_broker.submit_sell.return_value = {
        "status": "submitted",
        "filled_qty": Decimal("10"),
        "filled_avg_price": Decimal("120"),
    }

    # 20% drift — would have been rejected under the pre-C3 logic
    with patch("scorched.services.trade_execution._fetch_live_price_single", return_value=Decimal("120.0")), \
         patch("scorched.services.trade_execution.get_broker", return_value=fake_broker):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/api/v1/trades/confirm", json={"recommendation_id": rec.id})

    assert r.status_code == 200
    fake_broker.submit_sell.assert_called_once()


@pytest.mark.asyncio
async def test_confirm_sell_uses_sell_buffer_below_live(db_session, _override_db):
    """Sell limit price = live * (1 - sell_buffer_pct/100)."""
    rec = await _make_rec(db_session, action="sell", suggested_price=Decimal("150.00"))

    fake_broker = AsyncMock()
    fake_broker.submit_sell.return_value = {
        "status": "submitted",
        "filled_qty": Decimal("10"),
        "filled_avg_price": Decimal("149.55"),
    }

    with patch("scorched.services.trade_execution._fetch_live_price_single", return_value=Decimal("150.0")), \
         patch("scorched.services.trade_execution.get_broker", return_value=fake_broker):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/api/v1/trades/confirm", json={"recommendation_id": rec.id})

    assert r.status_code == 200
    fake_broker.submit_sell.assert_called_once()
    kwargs = fake_broker.submit_sell.call_args.kwargs
    # 150 * (1 - 0.003) = 149.55
    assert kwargs["limit_price"] == Decimal("149.55")
