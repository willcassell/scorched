"""Shared position reconciliation logic — local DB vs broker."""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..broker import get_broker
from ..config import settings
from ..models import Position


async def check_reconciliation(db: AsyncSession) -> dict:
    """Compare local DB positions against broker holdings.

    Returns:
        {
            "positions": [{"symbol": ..., "broker_qty": ..., "local_qty": ..., "status": "OK"|"MISMATCH"}, ...],
            "has_mismatches": bool,
            "mismatches": [{"symbol": ..., "broker_qty": ..., "local_qty": ...}, ...],
        }
    Returns {"positions": [], "has_mismatches": False, "mismatches": []} for paper mode.
    """
    if settings.broker_mode not in ("alpaca_paper", "alpaca_live"):
        return {"positions": [], "has_mismatches": False, "mismatches": []}

    broker = get_broker(db)
    broker_positions = await broker.get_positions()
    local_positions = (await db.execute(select(Position))).scalars().all()

    broker_map = {p["symbol"]: p for p in broker_positions}
    local_map = {p.symbol: p for p in local_positions}

    all_symbols = set(broker_map.keys()) | set(local_map.keys())
    diffs = []
    mismatches = []

    for sym in sorted(all_symbols):
        b = broker_map.get(sym)
        l = local_map.get(sym)
        broker_qty = b["qty"] if b else Decimal("0")
        local_qty = l.shares if l else Decimal("0")

        status = "MISMATCH" if broker_qty != local_qty else "OK"
        entry = {
            "symbol": sym,
            "broker_qty": str(broker_qty),
            "local_qty": str(local_qty),
            "status": status,
        }
        diffs.append(entry)
        if status == "MISMATCH":
            mismatches.append(entry)

    return {
        "positions": diffs,
        "has_mismatches": len(mismatches) > 0,
        "mismatches": mismatches,
    }
