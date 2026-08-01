"""Exit-reason telemetry — apply_sell persists exit_reason/exit_trigger on TradeHistory.

Motivation: 23 of the last 31 sells were intraday auto-exits with NO reason
recorded anywhere, so the loss engine couldn't be tuned. These fields let us
attribute every sell to its cause: "recommendation" (Phase 2), "intraday_hard_stop",
"intraday_claude_exit", or "manual", plus (for intraday sells) which of the 6
trigger types fired.
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from scorched.models import Position, TradeHistory
from scorched.services.portfolio import apply_sell


@pytest_asyncio.fixture
async def seeded_position(db_session):
    """A held AAPL position so apply_sell has something to sell."""
    pos = Position(
        symbol="AAPL",
        shares=Decimal("10"),
        avg_cost_basis=Decimal("150.00"),
        first_purchase_date=datetime.now(timezone.utc).date(),
        high_water_mark=Decimal("150.00"),
        trailing_stop_price=Decimal("142.50"),
    )
    db_session.add(pos)
    await db_session.commit()
    return pos


@pytest.mark.asyncio
async def test_apply_sell_persists_exit_reason(db_session, seeded_position):
    await apply_sell(
        db_session,
        recommendation_id=None,
        symbol="AAPL",
        shares=Decimal("5"),
        execution_price=Decimal("140.00"),
        executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        exit_reason="intraday_hard_stop",
        exit_trigger="position_drop_from_entry",
    )
    row = (
        await db_session.execute(select(TradeHistory).where(TradeHistory.action == "sell"))
    ).scalar_one()
    assert row.exit_reason == "intraday_hard_stop"
    assert row.exit_trigger == "position_drop_from_entry"


@pytest.mark.asyncio
async def test_apply_sell_defaults_exit_fields_to_none(db_session, seeded_position):
    """Callers that don't pass exit_reason/exit_trigger (legacy paths) get NULL, not a crash."""
    await apply_sell(
        db_session,
        recommendation_id=None,
        symbol="AAPL",
        shares=Decimal("5"),
        execution_price=Decimal("140.00"),
        executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    row = (
        await db_session.execute(select(TradeHistory).where(TradeHistory.action == "sell"))
    ).scalar_one()
    assert row.exit_reason is None
    assert row.exit_trigger is None
