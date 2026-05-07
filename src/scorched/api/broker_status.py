"""Broker status, position reconciliation, and sync endpoints."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..broker import get_broker
from ..broker.pending_fills import (
    DEFAULT_STALE_AGE_HOURS,
    release_stale_pending_fills,
)
from ..config import settings
from ..database import get_db
from ..services.reconciliation import check_reconciliation, sync_positions
from ..services.telegram import send_telegram
from .deps import require_owner_pin

router = APIRouter(prefix="/broker", tags=["broker"], dependencies=[Depends(require_owner_pin)])


@router.get("/status")
async def broker_status(db: AsyncSession = Depends(get_db)):
    """Return broker mode, account info, and position reconciliation."""
    broker = get_broker(db)
    account = await broker.get_account()

    recon = await check_reconciliation(db)

    return {
        "broker_mode": settings.broker_mode,
        "account": account,
        "reconciliation": recon,
    }


@router.post("/sync", dependencies=[Depends(require_owner_pin)])
async def broker_sync(db: AsyncSession = Depends(get_db)):
    """Sync local DB positions to match Alpaca holdings.

    Alpaca is the source of truth. Fixes quantity mismatches,
    adds missing positions, removes stale ones.
    """
    return await sync_positions(db)


@router.post("/release-stale-pending-fills", dependencies=[Depends(require_owner_pin)])
async def broker_release_stale_pending_fills(
    age_hours: int = Query(DEFAULT_STALE_AGE_HOURS, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
):
    """Release pending fills older than `age_hours`.

    Daily cron entry-point — runs after Phase 2.5 reconcile so anything still
    pending is something the reconciler couldn't resolve. Releasing the local
    row only frees the cash reservation; the next reconcile cycle recovers
    real Alpaca orders via stored client_order_id.
    """
    released = await release_stale_pending_fills(db, age_hours=age_hours)
    if released:
        lines = [f"TRADEBOT // RELEASED {len(released)} STALE RESERVATION(S)"]
        for r in released:
            age_h = (r.get("age_seconds") or 0) / 3600
            lines.append(
                f"  - {r['action'].upper()} {r['symbol']} {r['qty']}sh @ ${r['limit_price']} "
                f"(age {age_h:.1f}h, client_oid={r['client_order_id']})"
            )
        try:
            await send_telegram("\n".join(lines))
        except Exception:  # noqa: BLE001 — Telegram is best-effort
            pass
    return {
        "released_count": len(released),
        "age_hours": age_hours,
        "released": released,
    }
