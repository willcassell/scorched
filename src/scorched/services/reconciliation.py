"""Shared position + cash reconciliation logic — local DB vs broker.

Alpaca is the source of truth for BOTH positions AND cash. Position sync fixes
qty/cost-basis mismatches without touching cash; a separate cash reconciliation
step reads Alpaca's actual `account.cash` and replaces `Portfolio.cash_balance`
if drift exceeds the threshold. This avoids the prior bug where the position-
sync loop synthesized cash adjustments from cost basis (which is wrong whenever
the market price differs from cost basis).
"""
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

# Anything smaller is rounding noise (sub-dollar). Drifts at or above this
# threshold are reported as DRIFT on /broker/status and corrected by sync.
CASH_DRIFT_THRESHOLD = Decimal("1.00")


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


async def _compute_cash_info(db: AsyncSession, broker) -> dict:
    """Read Alpaca account.cash and compare to local Portfolio.cash_balance.

    Returns a dict safe to embed in API responses; on error returns an
    ERROR-status entry so the caller can still render the position section.
    """
    portfolio = (await db.execute(select(Portfolio))).scalars().first()
    local_cash = portfolio.cash_balance if portfolio else Decimal("0")
    try:
        account = await broker.get_account()
        alpaca_cash = Decimal(str(account["cash"])).quantize(Decimal("0.01"))
        cash_diff = (alpaca_cash - local_cash).quantize(Decimal("0.01"))
        cash_diff_pct = (
            float((cash_diff / local_cash * 100).quantize(Decimal("0.01")))
            if local_cash and local_cash != 0 else 0.0
        )
        cash_status = "OK" if abs(cash_diff) < CASH_DRIFT_THRESHOLD else "DRIFT"
        return {
            "local": float(local_cash),
            "broker": float(alpaca_cash),
            "diff": float(cash_diff),
            "diff_pct": cash_diff_pct,
            "status": cash_status,
        }
    except Exception:
        logger.warning("Cash reconciliation check failed", exc_info=True)
        return {
            "local": float(local_cash),
            "broker": None,
            "diff": None,
            "diff_pct": None,
            "status": "ERROR",
        }


async def check_reconciliation(db: AsyncSession) -> dict:
    """Compare local DB positions AND cash against broker.

    Returns:
        {
            "positions": [{"symbol", "broker_qty", "local_qty", "status"}, ...],
            "has_mismatches": bool,   # true if any position OR cash drift
            "mismatches": [...],       # position mismatches only
            "cash": {"local", "broker", "diff", "diff_pct", "status"},
        }
    Returns an empty skeleton for paper mode.
    """
    if settings.broker_mode not in ("alpaca_paper", "alpaca_live"):
        return {
            "positions": [],
            "has_mismatches": False,
            "mismatches": [],
            "cash": {"local": 0, "broker": 0, "diff": 0, "diff_pct": 0, "status": "N/A"},
        }

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

    cash_info = await _compute_cash_info(db, broker)
    cash_drift = cash_info["status"] == "DRIFT"

    return {
        "positions": diffs,
        "has_mismatches": len(mismatches) > 0 or cash_drift,
        "mismatches": mismatches,
        "cash": cash_info,
    }


async def sync_positions(db: AsyncSession) -> dict:
    """Sync local DB to match Alpaca (source of truth for positions AND cash).

    Two-phase:
      1. Position loop — add/remove/adjust qty and cost basis only. Cash is
         NOT touched here (previous cost-basis math caused drift).
      2. Cash reconciliation — read Alpaca account.cash and replace
         Portfolio.cash_balance if drift exceeds CASH_DRIFT_THRESHOLD.

    Returns a summary of corrections made.
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
    corrections: list[dict] = []
    today = datetime.now(timezone.utc).replace(tzinfo=None).date()

    portfolio = (await db.execute(select(Portfolio))).scalars().first()

    # DB columns are Numeric(15,4) and Numeric(15,6) — round Alpaca values
    # to match DB precision so we don't detect phantom drifts every run.
    DB_QTY_PRECISION = Decimal("0.000001")   # Numeric(15,6)
    DB_PRICE_PRECISION = Decimal("0.0001")   # Numeric(15,4)

    # ── Position loop — fix qty/cost only, DO NOT touch cash here ─────────
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
            corrections.append({
                "symbol": sym,
                "action": "added",
                "detail": f"{broker_qty}sh @ ${broker_avg}",
            })
            logger.info("Sync: added %s — %s shares @ $%s", sym, broker_qty, broker_avg)

        elif broker_qty == 0 and local_qty > 0:
            # Local has it, Alpaca doesn't — drop local position.
            # No cash adjustment here — the cash reconciliation step below
            # uses Alpaca's actual cash balance, so any proceeds already
            # credited on Alpaca will show up there.
            await db.delete(local)
            corrections.append({
                "symbol": sym,
                "action": "removed",
                "detail": f"removed {local_qty}sh (Alpaca no longer holds)",
            })
            logger.info("Sync: removed %s — %s shares (no Alpaca position)", sym, local_qty)

        else:
            # Both have it but qty differs — adjust local to match Alpaca
            old_qty = local_qty
            local.shares = broker_qty
            local.avg_cost_basis = broker_avg
            corrections.append({
                "symbol": sym,
                "action": "adjusted_qty",
                "detail": f"{old_qty} → {broker_qty} shares (avg ${broker_avg})",
            })
            logger.info("Sync: %s qty %s → %s", sym, old_qty, broker_qty)

    # ── Cash reconciliation — Alpaca is source of truth ───────────────────
    # Runs AFTER position sync so position corrections don't confuse the diff.
    cash_correction_detail: dict | None = None
    try:
        account = await broker.get_account()
        alpaca_cash = Decimal(str(account["cash"])).quantize(Decimal("0.01"))
        local_cash = portfolio.cash_balance
        cash_diff = (alpaca_cash - local_cash).quantize(Decimal("0.01"))

        if abs(cash_diff) >= CASH_DRIFT_THRESHOLD:
            portfolio.cash_balance = alpaca_cash
            cash_correction_detail = {
                "local_before": float(local_cash),
                "alpaca": float(alpaca_cash),
                "diff": float(cash_diff),
            }
            corrections.append({
                "symbol": "CASH",
                "action": "cash_reconciled",
                "detail": f"${local_cash} → ${alpaca_cash} (diff ${cash_diff:+.2f})",
            })
            logger.warning(
                "Sync: cash drift corrected — local $%s → Alpaca $%s (diff $%+.2f)",
                local_cash, alpaca_cash, cash_diff,
            )
    except Exception:
        logger.exception("Sync: cash reconciliation step failed — positions synced, cash unchanged")

    # Record the sync call in api_call_log
    sync_status = "synced" if corrections else "in_sync"
    elapsed_ms = int((time.monotonic() - sync_start) * 1000)
    _record_sync_call(db, "sync_positions", "success", elapsed_ms)

    await db.commit()

    return {
        "status": sync_status,
        "corrections": corrections,
        "position_count": len([s for s in all_symbols if broker_map.get(s)]),
        "cash_correction": cash_correction_detail,
    }
