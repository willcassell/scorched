"""Track pending fills in the database for crash recovery and transaction integrity.

A pending fill is written BEFORE submitting to Alpaca (with client_order_id,
no order_id yet).  After Alpaca accepts, the record is updated with the real
order_id.  Once the fill is recorded via apply_buy/apply_sell, the pending
record is deleted — in the same DB session so both share a transaction boundary.

If the process crashes between Alpaca order submission and DB recording, the
startup reconciliation in main.py replays unrecorded fills using the
client_order_id to look up orders on Alpaca.
"""
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PendingFill

logger = logging.getLogger(__name__)


async def write_pending_fill(
    db: AsyncSession,
    *,
    client_order_id: str | None,
    symbol: str,
    action: str,
    qty: Decimal,
    limit_price: Decimal,
    recommendation_id: int | None,
) -> PendingFill:
    """Write a pending fill BEFORE submitting to Alpaca.

    The order_id is not yet known — it will be set after Alpaca accepts.
    We commit here so the record survives a crash during Alpaca submission.
    """
    fill = PendingFill(
        client_order_id=client_order_id,
        symbol=symbol,
        action=action,
        qty=qty,
        limit_price=limit_price,
        recommendation_id=recommendation_id,
    )
    db.add(fill)
    await db.commit()
    await db.refresh(fill)
    logger.info(
        "Wrote pending fill: client_oid=%s %s %s x%s @ %s",
        client_order_id, action, symbol, qty, limit_price,
    )
    return fill


async def update_pending_fill_order_id(
    db: AsyncSession,
    *,
    client_order_id: str,
    order_id: str,
) -> None:
    """Update a pending fill with the real Alpaca order ID after submission."""
    result = await db.execute(
        select(PendingFill).where(PendingFill.client_order_id == client_order_id)
    )
    fill = result.scalars().first()
    if fill:
        fill.order_id = order_id
        await db.commit()
        logger.info("Updated pending fill: client_oid=%s → order_id=%s", client_order_id, order_id)
    else:
        logger.warning("No pending fill found for client_oid=%s to update", client_order_id)


async def remove_pending_fill(db: AsyncSession, order_id: str) -> None:
    """Remove a pending fill after successful DB recording.

    Does NOT commit — the caller should commit as part of a larger transaction
    (e.g., after apply_buy/apply_sell) so both operations are atomic.
    """
    result = await db.execute(
        select(PendingFill).where(PendingFill.order_id == order_id)
    )
    fill = result.scalars().first()
    if fill:
        await db.delete(fill)
        logger.info("Marked pending fill for removal: order=%s", order_id)
    else:
        logger.debug("Pending fill not found for removal: order=%s", order_id)


async def remove_pending_fill_by_client_oid(db: AsyncSession, client_order_id: str) -> None:
    """Remove a pending fill by client_order_id (for pre-submission failures)."""
    result = await db.execute(
        select(PendingFill).where(PendingFill.client_order_id == client_order_id)
    )
    fill = result.scalars().first()
    if fill:
        await db.delete(fill)
        await db.commit()
        logger.info("Removed pending fill by client_oid=%s", client_order_id)


async def get_pending_fills(db: AsyncSession) -> list[dict]:
    """Return all pending fills (used by startup reconciliation)."""
    result = await db.execute(select(PendingFill))
    return [
        {
            "order_id": f.order_id,
            "client_order_id": f.client_order_id,
            "symbol": f.symbol,
            "action": f.action,
            "qty": str(f.qty),
            "limit_price": str(f.limit_price),
            "recommendation_id": f.recommendation_id,
        }
        for f in result.scalars().all()
    ]
