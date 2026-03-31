"""Broker status and position reconciliation endpoint."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..broker import get_broker
from ..config import settings
from ..database import get_db
from ..services.reconciliation import check_reconciliation

router = APIRouter(prefix="/broker", tags=["broker"])


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
