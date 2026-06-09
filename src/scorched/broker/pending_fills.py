"""Track pending fills in the database for crash recovery and transaction integrity.

A pending fill is written BEFORE submitting to Alpaca (with client_order_id,
no order_id yet).  After Alpaca accepts, the record is updated with the real
order_id.  Once the fill is recorded via apply_buy/apply_sell, the pending
record is deleted — in the same DB session so both share a transaction boundary.

If the process crashes between Alpaca order submission and DB recording, the
startup reconciliation in main.py replays unrecorded fills using the
client_order_id to look up orders on Alpaca.

Lifecycle invariant: every reservation MUST be released eventually. The
release-stale path (`release_stale_pending_fills`) backstops every async
failure mode the regular reconciler doesn't catch — broker reject after
network drop, container restart loop mid-submit, persistent Alpaca 5xx, etc.
Without it, `get_pending_buy_notional()` silently strands cash.
"""
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PendingFill

logger = logging.getLogger(__name__)


# Day-orders expire end-of-session; six hours past intended fill time is
# already long enough that something is wrong. Configurable through the
# release_stale_pending_fills argument.
DEFAULT_STALE_AGE_HOURS = 6


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

    Dedup: if a row already exists for the same client_order_id, reuse it
    instead of inserting a duplicate. This pairs with AlpacaBroker's
    idempotent recovery on error 40010001 — without dedup, a Phase 2 retry
    or intraday re-fire would leave two pending_fill rows pointing at the
    same Alpaca order, causing the reconciler to apply the fill twice for
    recommendation_id=None (intraday) sells.
    """
    if client_order_id:
        existing = (await db.execute(
            select(PendingFill).where(PendingFill.client_order_id == client_order_id)
        )).scalars().first()
        if existing is not None:
            logger.info(
                "Pending fill already exists for client_oid=%s — reusing row id=%s",
                client_order_id, existing.id,
            )
            return existing
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


async def get_pending_buy_notional(
    db: AsyncSession,
    exclude_recommendation_id: int | None = None,
) -> Decimal:
    """Return total notional reserved by outstanding pending buy orders.

    Pending fills are active until reconciliation records the fill and removes
    the row. During that window, Scorched should treat buy notional as already
    reserved so a second confirmation cannot independently spend the same cash.

    `exclude_recommendation_id` lets an idempotent re-confirm of an already
    "submitted" rec skip its OWN pending fill — otherwise the retry double-debits
    its notional and can self-reject on the cash floor.
    """
    result = await db.execute(select(PendingFill).where(PendingFill.action == "buy"))
    total = Decimal("0")
    for fill in result.scalars().all():
        if (
            exclude_recommendation_id is not None
            and fill.recommendation_id == exclude_recommendation_id
        ):
            continue
        total += Decimal(str(fill.qty)) * Decimal(str(fill.limit_price))
    return total


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
            "created_at": f.created_at.isoformat() if f.created_at else None,
        }
        for f in result.scalars().all()
    ]


async def get_pending_fills_summary(db: AsyncSession) -> dict:
    """Snapshot of pending-fill state for operator observability.

    Distinguishes "fresh" (created within the stale threshold) from "stale"
    (older than threshold and almost certainly leaked) so the dashboard can
    surface stranded reservations before they distort the cash gate.
    """
    fills = (await db.execute(select(PendingFill))).scalars().all()
    now = datetime.utcnow()
    threshold = now - timedelta(hours=DEFAULT_STALE_AGE_HOURS)

    fresh: list[dict] = []
    stale: list[dict] = []
    reserved_buy = Decimal("0")
    reserved_buy_stale = Decimal("0")

    for f in fills:
        notional = Decimal(str(f.qty)) * Decimal(str(f.limit_price))
        is_stale = f.created_at is not None and f.created_at < threshold
        row = {
            "id": f.id,
            "order_id": f.order_id,
            "client_order_id": f.client_order_id,
            "symbol": f.symbol,
            "action": f.action,
            "qty": str(f.qty),
            "limit_price": str(f.limit_price),
            "notional": str(notional),
            "recommendation_id": f.recommendation_id,
            "created_at": f.created_at.isoformat() if f.created_at else None,
            "age_seconds": (
                int((now - f.created_at).total_seconds()) if f.created_at else None
            ),
        }
        (stale if is_stale else fresh).append(row)
        if f.action == "buy":
            reserved_buy += notional
            if is_stale:
                reserved_buy_stale += notional

    return {
        "total_count": len(fills),
        "fresh_count": len(fresh),
        "stale_count": len(stale),
        "reserved_buy_notional": str(reserved_buy),
        "reserved_buy_stale_notional": str(reserved_buy_stale),
        "stale_age_threshold_hours": DEFAULT_STALE_AGE_HOURS,
        "fresh": fresh,
        "stale": stale,
    }


async def release_stale_pending_fills(
    db: AsyncSession,
    *,
    age_hours: int = DEFAULT_STALE_AGE_HOURS,
) -> list[dict]:
    """Forcibly release pending fills older than `age_hours` and report what was released.

    This is the lifecycle backstop. The regular reconciler (`reconcile_pending_orders`)
    is responsible for resolving every fill; this function is only safe to call
    AFTER reconcile has already run, so anything still pending is something the
    reconciler couldn't resolve.

    A released reservation does NOT cancel an order on Alpaca — if the order
    actually exists, the next reconcile cycle will recover it via the
    client_order_id lookup. Releasing the local row only stops the reservation
    from blocking new buys against `get_pending_buy_notional()`.
    """
    cutoff = datetime.utcnow() - timedelta(hours=age_hours)
    result = await db.execute(
        select(PendingFill).where(PendingFill.created_at < cutoff)
    )
    stale = list(result.scalars().all())

    released: list[dict] = []
    for fill in stale:
        notional = Decimal(str(fill.qty)) * Decimal(str(fill.limit_price))
        released.append({
            "id": fill.id,
            "symbol": fill.symbol,
            "action": fill.action,
            "qty": str(fill.qty),
            "limit_price": str(fill.limit_price),
            "notional": str(notional),
            "client_order_id": fill.client_order_id,
            "order_id": fill.order_id,
            "recommendation_id": fill.recommendation_id,
            "age_seconds": (
                int((datetime.utcnow() - fill.created_at).total_seconds())
                if fill.created_at else None
            ),
        })
        await db.delete(fill)
    if stale:
        await db.commit()
        logger.warning(
            "Released %d stale pending fill(s) older than %dh — "
            "regular reconcile did not resolve them. Symbols: %s",
            len(stale), age_hours,
            ", ".join(f"{r['action']}:{r['symbol']}" for r in released),
        )
    return released
