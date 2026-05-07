"""Lifecycle tests for pending_fills — release-stale + summary observability.

Codex's adversarial review flagged that pending reservations can strand cash
when the regular reconciler can't resolve them (Alpaca persistent error,
container restart mid-submit). The release-stale backstop and the summary
endpoint exist so an operator can see and recover from those situations.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from scorched.api.deps import require_owner_pin
from scorched.broker.pending_fills import (
    DEFAULT_STALE_AGE_HOURS,
    get_pending_buy_notional,
    get_pending_fills_summary,
    release_stale_pending_fills,
)
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
    fill = PendingFill(
        symbol=symbol, action=action, qty=Decimal(str(qty)),
        limit_price=Decimal(str(limit_price)),
        client_order_id=f"oid-{symbol}-{action}",
        order_id=None,
        recommendation_id=None,
        created_at=datetime.utcnow() - timedelta(hours=age_hours),
    )
    db_session.add(fill)
    await db_session.commit()
    return fill


@pytest.mark.asyncio
async def test_summary_distinguishes_fresh_from_stale(db_session):
    """`stale_count` and `reserved_buy_stale_notional` must reflect orphans."""
    await _seed_pending(db_session, symbol="AAPL", action="buy", qty=10,
                        limit_price=150, age_hours=1)
    await _seed_pending(db_session, symbol="MSFT", action="buy", qty=5,
                        limit_price=300, age_hours=DEFAULT_STALE_AGE_HOURS + 2)

    summary = await get_pending_fills_summary(db_session)
    assert summary["total_count"] == 2
    assert summary["fresh_count"] == 1
    assert summary["stale_count"] == 1
    # Reserved buy notional is the SUM of both fresh and stale buys.
    assert Decimal(summary["reserved_buy_notional"]) == Decimal("3000")
    assert Decimal(summary["reserved_buy_stale_notional"]) == Decimal("1500")


@pytest.mark.asyncio
async def test_release_stale_drops_only_old_rows(db_session):
    """Fresh reservations must be untouched; stale ones removed."""
    await _seed_pending(db_session, symbol="AAPL", action="buy", qty=10,
                        limit_price=150, age_hours=1)
    await _seed_pending(db_session, symbol="MSFT", action="sell", qty=5,
                        limit_price=300, age_hours=DEFAULT_STALE_AGE_HOURS + 2)

    released = await release_stale_pending_fills(db_session)
    assert len(released) == 1
    assert released[0]["symbol"] == "MSFT"
    assert released[0]["action"] == "sell"
    assert released[0]["age_seconds"] is not None and released[0]["age_seconds"] > 0

    summary = await get_pending_fills_summary(db_session)
    assert summary["total_count"] == 1
    assert summary["fresh_count"] == 1
    assert summary["stale_count"] == 0


@pytest.mark.asyncio
async def test_release_stale_is_noop_when_nothing_stale(db_session):
    """Empty result list when no row is older than threshold."""
    await _seed_pending(db_session, symbol="AAPL", action="buy", qty=10,
                        limit_price=150, age_hours=1)
    released = await release_stale_pending_fills(db_session)
    assert released == []


@pytest.mark.asyncio
async def test_release_stale_frees_buy_notional_for_next_gate_check(db_session):
    """After release, `get_pending_buy_notional` reflects the freed reservation.

    Without this, a leaked reservation would permanently inflate the
    'effective cash' subtraction in trade_execution.py, mimicking a
    catalyst-hunter strategy bug — exactly the symptom Codex described.
    """
    await _seed_pending(db_session, symbol="STALE", action="buy", qty=20,
                        limit_price=200, age_hours=DEFAULT_STALE_AGE_HOURS + 5)
    pre = await get_pending_buy_notional(db_session)
    assert pre == Decimal("4000")

    await release_stale_pending_fills(db_session)
    post = await get_pending_buy_notional(db_session)
    assert post == Decimal("0")


@pytest.mark.asyncio
async def test_release_stale_respects_custom_age_threshold(db_session):
    """Caller can tighten or loosen the threshold to scope releases."""
    await _seed_pending(db_session, symbol="A", action="buy", qty=1,
                        limit_price=100, age_hours=2)
    # Tighter threshold (1 hour) catches the 2h-old row.
    released = await release_stale_pending_fills(db_session, age_hours=1)
    assert len(released) == 1


@pytest.mark.asyncio
async def test_pending_fills_endpoint_returns_summary(db_session, _override_db):
    await _seed_pending(db_session, symbol="GOOG", action="buy", qty=2,
                        limit_price=180, age_hours=DEFAULT_STALE_AGE_HOURS + 1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/api/v1/system/pending-fills")
    assert r.status_code == 200
    body = r.json()
    assert body["total_count"] == 1
    assert body["stale_count"] == 1
    assert body["stale_age_threshold_hours"] == DEFAULT_STALE_AGE_HOURS
