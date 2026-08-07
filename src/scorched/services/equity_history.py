"""Daily portfolio equity snapshots.

The `portfolio` table is a single row updated in place, so it carries no
history. Reconstructing past equity or invested-% from `trade_history` is not
safe — that record has been shown to be incomplete when a pending-fill
reconcile errors and `broker/sync` absorbs the exit as a bare cash correction
(see .handovers/2026-08-06-performance-review.md §2).

This module writes one durable row per NYSE trading day so questions like
"was the bot chronically underinvested, or only during Experiment C?" can be
answered with a plain SELECT.
"""
import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import EquityHistory, Portfolio, TradeHistory
from ..tz import market_today
from .portfolio import get_portfolio_state

logger = logging.getLogger(__name__)


async def _broker_equity(db: AsyncSession) -> Decimal | None:
    """Broker-reported equity, or None. Best-effort — never raises.

    Only meaningful in alpaca_* modes; PaperBroker's `equity` is just the cash
    balance, which would be a misleading value to store next to `total_value`.
    """
    if settings.broker_mode not in ("alpaca_paper", "alpaca_live"):
        return None
    try:
        from ..broker import get_broker

        account = await get_broker(db).get_account()
        raw = account.get("equity")
        return Decimal(str(raw)) if raw is not None else None
    except Exception as exc:  # noqa: BLE001 — telemetry must not break the caller
        logger.warning("Equity snapshot: broker equity unavailable (%s)", exc)
        return None


async def record_snapshot(
    db: AsyncSession, snapshot_date: date | None = None
) -> EquityHistory | None:
    """Write (or update) today's equity snapshot. Returns the row, or None on failure.

    Idempotent: `snapshot_date` is unique, so a Phase 3 re-run updates the
    existing row rather than duplicating it.

    Never raises — this is telemetry, and a snapshot failure must not take down
    the EOD review it is called from.
    """
    if snapshot_date is None:
        snapshot_date = market_today()

    try:
        state = await get_portfolio_state(db)

        realized = (
            await db.execute(
                select(func.coalesce(func.sum(TradeHistory.realized_gain), 0))
            )
        ).scalar_one()

        portfolio = (await db.execute(select(Portfolio))).scalars().first()
        starting_capital = portfolio.starting_capital if portfolio else Decimal("0")

        total_value = state.total_value
        invested_pct = (
            (state.total_positions_value / total_value * 100).quantize(Decimal("0.01"))
            if total_value
            else Decimal("0")
        )

        row = (
            await db.execute(
                select(EquityHistory).where(EquityHistory.snapshot_date == snapshot_date)
            )
        ).scalars().first()

        if row is None:
            row = EquityHistory(snapshot_date=snapshot_date)
            db.add(row)

        row.total_value = total_value
        row.cash_balance = state.cash_balance
        row.positions_value = state.total_positions_value
        row.invested_pct = invested_pct
        row.unrealized_gain = state.total_unrealized_gain
        row.realized_pnl_to_date = Decimal(str(realized))
        row.position_count = len(state.positions)
        row.starting_capital = starting_capital
        row.broker_equity = await _broker_equity(db)

        await db.commit()
        logger.info(
            "Equity snapshot %s: total=$%s invested=%s%% positions=%d",
            snapshot_date, total_value, invested_pct, row.position_count,
        )
        return row

    except Exception as exc:  # noqa: BLE001 — telemetry must not break the caller
        logger.exception("Equity snapshot failed for %s: %s", snapshot_date, exc)
        await db.rollback()
        return None


async def get_history(db: AsyncSession, days: int = 90) -> list[EquityHistory]:
    """Most recent `days` snapshots, oldest first."""
    rows = (
        await db.execute(
            select(EquityHistory).order_by(EquityHistory.snapshot_date.desc()).limit(days)
        )
    ).scalars().all()
    return list(reversed(rows))
