"""Regression tests for the broker/sync guard against negative + orphan broker qty.

Before 2026-08-07 a broker position with a negative qty and no matching local
row fell through to the "both have it but qty differs" branch and executed
`local.shares = broker_qty` with `local = None`, raising AttributeError. That
aborted the position loop AND the cash reconciliation that runs after it, so one
phantom short silently stopped all daily reconciliation (observed 2026-08-06:
both the 10:45 and 14:00 syncs failed with HTTP 500).
"""
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from scorched.models import Position
from scorched.services.reconciliation import sync_positions


def _broker(positions, cash="1000.00"):
    """Fake broker returning the given position dicts."""
    b = AsyncMock()
    b.get_positions = AsyncMock(return_value=positions)
    b.get_account = AsyncMock(return_value={"cash": cash, "equity": cash, "status": "ACTIVE"})
    return b


@pytest.mark.asyncio
async def test_negative_broker_qty_does_not_raise(db_session):
    """A short on the broker with no local row must not crash the sync."""
    broker = _broker([
        {
            "symbol": "HON",
            "qty": Decimal("-55"),
            "avg_cost_basis": Decimal("0"),
            "market_value": Decimal("-13690.60"),
            "unrealized_pl": Decimal("-13690.60"),
        }
    ])
    with patch("scorched.services.reconciliation.get_broker", return_value=broker), \
         patch("scorched.services.reconciliation.settings") as s:
        s.broker_mode = "alpaca_paper"
        result = await sync_positions(db_session)

    actions = [c["action"] for c in result["corrections"]]
    assert "unmanaged_short" in actions
    detail = next(c["detail"] for c in result["corrections"] if c["action"] == "unmanaged_short")
    assert "-55" in detail


@pytest.mark.asyncio
async def test_negative_broker_qty_creates_no_local_position(db_session):
    """The short must never be mirrored into `positions` — a negative Position
    row would corrupt every downstream valuation and gate."""
    broker = _broker([
        {
            "symbol": "HON",
            "qty": Decimal("-55"),
            "avg_cost_basis": Decimal("0"),
            "market_value": Decimal("-13690.60"),
            "unrealized_pl": Decimal("-13690.60"),
        }
    ])
    with patch("scorched.services.reconciliation.get_broker", return_value=broker), \
         patch("scorched.services.reconciliation.settings") as s:
        s.broker_mode = "alpaca_paper"
        await sync_positions(db_session)

    rows = (await db_session.execute(select(Position).where(Position.symbol == "HON"))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_short_does_not_block_other_symbols_or_cash(db_session):
    """The whole point: a short must not abort the loop for later symbols, nor
    the cash reconciliation that runs after it."""
    broker = _broker(
        [
            {
                "symbol": "HON",  # sorts before MSFT — must not stop the loop
                "qty": Decimal("-55"),
                "avg_cost_basis": Decimal("0"),
                "market_value": Decimal("-13690.60"),
                "unrealized_pl": Decimal("-13690.60"),
            },
            {
                "symbol": "MSFT",
                "qty": Decimal("10"),
                "avg_cost_basis": Decimal("400.00"),
                "market_value": Decimal("4200.00"),
                "unrealized_pl": Decimal("200.00"),
            },
        ],
        cash="777.77",
    )
    with patch("scorched.services.reconciliation.get_broker", return_value=broker), \
         patch("scorched.services.reconciliation.settings") as s:
        s.broker_mode = "alpaca_paper"
        result = await sync_positions(db_session)

    actions = {c["action"] for c in result["corrections"]}
    assert "unmanaged_short" in actions
    assert "added" in actions, "symbol sorting after the short was skipped"

    msft = (await db_session.execute(select(Position).where(Position.symbol == "MSFT"))).scalars().first()
    assert msft is not None and msft.shares == Decimal("10")

    # Cash reconciliation runs after the position loop — it must still have run.
    assert any(c["action"] == "cash_reconciled" for c in result["corrections"]), (
        "cash reconciliation did not run after the short was encountered"
    )
