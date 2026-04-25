"""Broker status, position reconciliation, and sync endpoints."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..broker import get_broker
from ..config import settings
from ..database import get_db
from ..services.reconciliation import check_reconciliation, sync_positions
from .deps import require_owner_pin

router = APIRouter(prefix="/broker", tags=["broker"])


@router.get("/status", dependencies=[Depends(require_owner_pin)])
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
