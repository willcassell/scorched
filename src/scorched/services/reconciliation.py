"""Shared position reconciliation logic — local DB vs broker."""
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..broker import get_broker
from ..config import settings
from ..models import ApiCallLog, Portfolio, Position

logger = logging.getLogger(__name__)


def _record_sync_call(db: AsyncSession, endpoint: str, status: str,
                      response_time_ms: int, error_message: str | None = None):
    """Queue a sync API call record (committed by the caller's commit)."""
    db.add(ApiCallLog(
        service="alpaca_sync",
        endpoint=endpoint,
        status=status,
        response_time_ms=response_time_ms,
        error_message=error_message,
    ))


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


async def sync_positions(db: AsyncSession) -> dict:
    """Sync local DB positions to match Alpaca (source of truth).

    Handles three mismatch types:
    1. Alpaca has position, local doesn't → create local position
    2. Local has position, Alpaca doesn't → remove local position
    3. Both have position but qty/cost differs → update local to match

    Returns summary of corrections made.
    """
    if settings.broker_mode not in ("alpaca_paper", "alpaca_live"):
        return {"status": "skipped", "reason": "paper mode", "corrections": []}

    sync_start = time.monotonic()
    broker = get_broker(db)
    broker_positions = await broker.get_positions()
    local_positions = (await db.execute(select(Position))).scalars().all()

    broker_map = {p["symbol"]: p for p in broker_positions}
    local_map = {p.symbol: p for p in local_positions}

    all_symbols = set(broker_map.keys()) | set(local_map.keys())
    corrections = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = now.date()

    portfolio = (await db.execute(select(Portfolio))).scalars().first()

    # DB columns are Numeric(15,4) and Numeric(15,6) — round Alpaca values
    # to match DB precision so we don't detect phantom drifts every run.
    DB_QTY_PRECISION = Decimal("0.000001")   # Numeric(15,6)
    DB_PRICE_PRECISION = Decimal("0.0001")   # Numeric(15,4)

    for sym in sorted(all_symbols):
        b = broker_map.get(sym)
        local = local_map.get(sym)
        broker_qty = b["qty"].quantize(DB_QTY_PRECISION) if b else Decimal("0")
        broker_avg = b["avg_cost_basis"].quantize(DB_PRICE_PRECISION) if b else Decimal("0")
        local_qty = local.shares if local else Decimal("0")

        if broker_qty == local_qty:
            # Qty matches — check if avg cost drifted
            if local and b and local.avg_cost_basis != broker_avg:
                old_avg = local.avg_cost_basis
                local.avg_cost_basis = broker_avg
                corrections.append({
                    "symbol": sym,
                    "action": "update_avg_cost",
                    "detail": f"avg cost {old_avg} → {broker_avg}",
                })
                logger.info("Sync: %s avg cost %s → %s", sym, old_avg, broker_avg)
            continue

        if broker_qty > 0 and local_qty == 0:
            # Alpaca has it, local doesn't — create position
            initial_stop = (broker_avg * Decimal("0.95")).quantize(Decimal("0.0001"))
            pos = Position(
                symbol=sym,
                shares=broker_qty,
                avg_cost_basis=broker_avg,
                first_purchase_date=today,
                high_water_mark=broker_avg,
                trailing_stop_price=initial_stop,
            )
            db.add(pos)
            # Deduct from cash (position value was on Alpaca, not tracked locally)
            cost = (broker_qty * broker_avg).quantize(Decimal("0.01"))
            portfolio.cash_balance -= cost
            corrections.append({
                "symbol": sym,
                "action": "added",
                "detail": f"{broker_qty}sh @ ${broker_avg} (cost ${cost})",
            })
            logger.info("Sync: added %s — %s shares @ $%s", sym, broker_qty, broker_avg)

        elif broker_qty == 0 and local_qty > 0:
            # Local has it, Alpaca doesn't — position was sold/closed on Alpaca
            proceeds = (local_qty * local.avg_cost_basis).quantize(Decimal("0.01"))
            portfolio.cash_balance += proceeds
            await db.delete(local)
            corrections.append({
                "symbol": sym,
                "action": "removed",
                "detail": f"removed {local_qty}sh (returned ${proceeds} to cash at cost basis)",
            })
            logger.info("Sync: removed %s — %s shares (no Alpaca position)", sym, local_qty)

        else:
            # Both have it but qty differs — adjust local to match Alpaca
            qty_diff = broker_qty - local_qty
            old_qty = local_qty
            local.shares = broker_qty
            local.avg_cost_basis = broker_avg
            # Adjust cash for the quantity difference
            cash_adj = (qty_diff * broker_avg).quantize(Decimal("0.01"))
            portfolio.cash_balance -= cash_adj
            corrections.append({
                "symbol": sym,
                "action": "adjusted_qty",
                "detail": f"{old_qty} → {broker_qty} shares (avg ${broker_avg}, cash adj ${cash_adj})",
            })
            logger.info("Sync: %s qty %s → %s", sym, old_qty, broker_qty)

    # Record the sync call in api_call_log
    sync_status = "synced" if corrections else "in_sync"
    elapsed_ms = int((time.monotonic() - sync_start) * 1000)
    _record_sync_call(db, "sync_positions", "success", elapsed_ms)

    if corrections:
        await db.commit()
    else:
        # Still commit the api_call_log record even when no corrections
        await db.commit()

    return {
        "status": sync_status,
        "corrections": corrections,
        "position_count": len([s for s in all_symbols if broker_map.get(s)]),
    }
