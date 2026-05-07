"""Endpoint test for /broker/release-stale-pending-fills.

Phase 2.5 cron calls this daily so a leaked reservation no longer waits for
a container restart to be cleared.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from scorched.api.deps import require_owner_pin
from scorched.broker.pending_fills import DEFAULT_STALE_AGE_HOURS
from scorched.database import get_db
from scorched.main import app
from scorched.models import PendingFill


@pytest.fixture
def _override_db(db_session):
    async def _get_db_override():
        yield db_session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[require_owner_pin] = lambda: None
    yield
    app.dependency_overrides.clear()


async def _seed_pending(db_session, *, symbol, action, qty, limit_price, age_hours):
    db_session.add(PendingFill(
        symbol=symbol, action=action, qty=Decimal(str(qty)),
        limit_price=Decimal(str(limit_price)),
        client_order_id=f"oid-{symbol}-{action}",
        recommendation_id=None,
        created_at=datetime.utcnow() - timedelta(hours=age_hours),
    ))
    await db_session.commit()


@pytest.mark.asyncio
async def test_release_stale_endpoint_returns_release_count(db_session, _override_db):
    await _seed_pending(db_session, symbol="STALE1", action="buy", qty=2,
                        limit_price=100, age_hours=DEFAULT_STALE_AGE_HOURS + 1)
    await _seed_pending(db_session, symbol="STALE2", action="sell", qty=3,
                        limit_price=80, age_hours=DEFAULT_STALE_AGE_HOURS + 5)
    await _seed_pending(db_session, symbol="FRESH", action="buy", qty=1,
                        limit_price=50, age_hours=1)

    with patch("scorched.api.broker_status.send_telegram", new=patch_send_telegram_noop()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/api/v1/broker/release-stale-pending-fills")

    assert r.status_code == 200
    body = r.json()
    assert body["released_count"] == 2
    released_symbols = {row["symbol"] for row in body["released"]}
    assert released_symbols == {"STALE1", "STALE2"}
    assert body["age_hours"] == DEFAULT_STALE_AGE_HOURS


@pytest.mark.asyncio
async def test_release_stale_endpoint_respects_custom_age_hours(db_session, _override_db):
    await _seed_pending(db_session, symbol="A", action="buy", qty=1,
                        limit_price=100, age_hours=2)

    with patch("scorched.api.broker_status.send_telegram", new=patch_send_telegram_noop()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/api/v1/broker/release-stale-pending-fills?age_hours=1")
    assert r.status_code == 200
    assert r.json()["released_count"] == 1
    assert r.json()["age_hours"] == 1


def patch_send_telegram_noop():
    """async-callable stub for send_telegram in tests."""
    async def _noop(*args, **kwargs):
        return None
    return _noop
