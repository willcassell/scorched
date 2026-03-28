"""Broker status and position reconciliation endpoint."""
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..broker import get_broker
from ..config import settings
from ..database import get_db
from ..models import Position

router = APIRouter(prefix="/broker", tags=["broker"])


@router.get("/status")
async def broker_status(db: AsyncSession = Depends(get_db)):
    """Return broker mode, account info, and position reconciliation."""
    broker = get_broker(db)
    account = await broker.get_account()

    result = {
        "broker_mode": settings.broker_mode,
        "account": account,
        "reconciliation": None,
    }

    # Position reconciliation: compare local DB vs broker
    if settings.broker_mode in ("alpaca_paper", "alpaca_live"):
        broker_positions = await broker.get_positions()
        local_positions = (await db.execute(select(Position))).scalars().all()

        broker_map = {p["symbol"]: p for p in broker_positions}
        local_map = {p.symbol: p for p in local_positions}

        all_symbols = set(broker_map.keys()) | set(local_map.keys())
        diffs = []

        for sym in sorted(all_symbols):
            b = broker_map.get(sym)
            l = local_map.get(sym)
            broker_qty = b["qty"] if b else Decimal("0")
            local_qty = l.shares if l else Decimal("0")

            if broker_qty != local_qty:
                diffs.append({
                    "symbol": sym,
                    "broker_qty": str(broker_qty),
                    "local_qty": str(local_qty),
                    "status": "MISMATCH",
                })
            else:
                diffs.append({
                    "symbol": sym,
                    "broker_qty": str(broker_qty),
                    "local_qty": str(local_qty),
                    "status": "OK",
                })

        result["reconciliation"] = {
            "positions": diffs,
            "has_mismatches": any(d["status"] == "MISMATCH" for d in diffs),
        }

    return result
