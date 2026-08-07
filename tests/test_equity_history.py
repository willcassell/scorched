"""Tests for daily equity snapshots (services/equity_history.py)."""
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from scorched.models import EquityHistory, Position, TradeHistory
from scorched.services.equity_history import get_history, record_snapshot


@pytest.fixture
def _prices():
    """Pin position pricing so snapshots are deterministic."""
    with patch(
        "scorched.services.portfolio._get_current_prices",
        AsyncMock(return_value={"MSFT": Decimal("110.00")}),
    ):
        yield


@pytest.mark.asyncio
async def test_snapshot_records_values_and_invested_pct(db_session, _prices):
    db_session.add(
        Position(
            symbol="MSFT",
            shares=Decimal("1"),
            avg_cost_basis=Decimal("100.00"),
            first_purchase_date=date(2026, 8, 1),
        )
    )
    await db_session.commit()

    row = await record_snapshot(db_session, date(2026, 8, 7))

    assert row is not None
    # seeded cash 1000 + 1sh @ 110
    assert row.total_value == Decimal("1110.00")
    assert row.cash_balance == Decimal("1000.0000")
    assert row.positions_value == Decimal("110.00")
    assert row.unrealized_gain == Decimal("10.00")
    assert row.position_count == 1
    # 110 / 1110 = 9.91%
    assert row.invested_pct == Decimal("9.91")


@pytest.mark.asyncio
async def test_snapshot_is_idempotent_per_day(db_session, _prices):
    await record_snapshot(db_session, date(2026, 8, 7))
    await record_snapshot(db_session, date(2026, 8, 7))

    rows = (await db_session.execute(select(EquityHistory))).scalars().all()
    assert len(rows) == 1, "re-running Phase 3 must update the day's row, not duplicate it"


@pytest.mark.asyncio
async def test_snapshot_updates_existing_row(db_session, _prices):
    first = await record_snapshot(db_session, date(2026, 8, 7))
    assert first.position_count == 0

    db_session.add(
        Position(
            symbol="MSFT",
            shares=Decimal("1"),
            avg_cost_basis=Decimal("100.00"),
            first_purchase_date=date(2026, 8, 1),
        )
    )
    await db_session.commit()

    second = await record_snapshot(db_session, date(2026, 8, 7))
    assert second.position_count == 1
    assert second.total_value == Decimal("1110.00")


@pytest.mark.asyncio
async def test_snapshot_sums_realized_pnl(db_session, _prices):
    for gain in (Decimal("100.00"), Decimal("-40.00")):
        db_session.add(
            TradeHistory(
                symbol="AAPL",
                action="sell",
                shares=Decimal("1"),
                execution_price=Decimal("10"),
                total_value=Decimal("10"),
                realized_gain=gain,
            )
        )
    await db_session.commit()

    row = await record_snapshot(db_session, date(2026, 8, 7))
    assert row.realized_pnl_to_date == Decimal("60.00")


@pytest.mark.asyncio
async def test_snapshot_never_raises(db_session):
    """Telemetry must not be able to take down the EOD review that calls it."""
    with patch(
        "scorched.services.equity_history.get_portfolio_state",
        AsyncMock(side_effect=RuntimeError("pricing exploded")),
    ):
        assert await record_snapshot(db_session, date(2026, 8, 7)) is None


@pytest.mark.asyncio
async def test_broker_equity_null_in_paper_mode(db_session, _prices):
    """PaperBroker reports cash as `equity`; storing that next to total_value
    would be misleading, so it must stay NULL."""
    with patch("scorched.services.equity_history.settings") as s:
        s.broker_mode = "paper"
        row = await record_snapshot(db_session, date(2026, 8, 7))
    assert row.broker_equity is None


@pytest.mark.asyncio
async def test_get_history_returns_oldest_first(db_session, _prices):
    for d in (date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7)):
        await record_snapshot(db_session, d)

    rows = await get_history(db_session, days=90)
    assert [r.snapshot_date for r in rows] == [
        date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7)
    ]


@pytest.mark.asyncio
async def test_get_history_days_limit_keeps_most_recent(db_session, _prices):
    for d in (date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7)):
        await record_snapshot(db_session, d)

    rows = await get_history(db_session, days=2)
    assert [r.snapshot_date for r in rows] == [date(2026, 8, 6), date(2026, 8, 7)]
