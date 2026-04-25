import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import Position
from ..schemas import ConfirmTradeRequest, ConfirmTradeResponse, PositionDetail, RejectTradeRequest, RejectTradeResponse
from ..models import TradeRecommendation
from .deps import require_owner_pin
from ..services.trade_execution import validate_and_submit_trade

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trades", tags=["trades"])


@router.post("/confirm", response_model=ConfirmTradeResponse, dependencies=[Depends(require_owner_pin)])
async def confirm_trade(body: ConfirmTradeRequest, db: AsyncSession = Depends(get_db)):
    """Confirm a recommended trade. Server decides price and quantity — client input is ignored.

    Routes through validate_and_submit_trade (shared with MCP confirm_trade) so both
    transports enforce the same audit C1 contract: live drift check, gate re-run, broker
    idempotency, and PIN protection.
    """
    try:
        result = await validate_and_submit_trade(body.recommendation_id, db)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("already:"):
            raise HTTPException(status_code=409, detail=f"Recommendation is already {msg[8:]}")
        if msg.startswith("drift:"):
            raise HTTPException(status_code=422, detail=f"Buy price drift {msg[6:]}")
        if msg.startswith("live price:"):
            raise HTTPException(status_code=503, detail=msg[11:])
        if msg.startswith("gate:"):
            raise HTTPException(status_code=422, detail=f"Risk gate rejected at confirm time: {msg[5:]}")
        raise HTTPException(status_code=422, detail=msg)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if result.status == "submitted":
        return ConfirmTradeResponse(
            trade_id=result.trade_id or 0,
            symbol=result.symbol,
            action=result.action,
            shares=result.filled_qty,
            execution_price=result.filled_price,
            total_value=Decimal("0"),
            new_cash_balance=Decimal("0"),
            position=None,
            realized_gain=None,
            tax_category=None,
        )

    pos = (await db.execute(
        select(Position).where(Position.symbol == result.symbol)
    )).scalars().first()

    position_detail = None
    if pos:
        position_detail = PositionDetail(
            symbol=pos.symbol,
            shares=pos.shares,
            avg_cost_basis=pos.avg_cost_basis,
            first_purchase_date=pos.first_purchase_date,
        )

    return ConfirmTradeResponse(
        trade_id=result.trade_id or 0,
        symbol=result.symbol,
        action=result.action,
        shares=result.filled_qty,
        execution_price=result.filled_price,
        total_value=(result.filled_qty * result.filled_price).quantize(Decimal("0.01")),
        new_cash_balance=result.new_cash_balance or Decimal("0"),
        position=position_detail,
        realized_gain=result.realized_gain,
        tax_category=result.tax_category,
    )


@router.post("/reconcile", dependencies=[Depends(require_owner_pin)])
async def reconcile_orders(db: AsyncSession = Depends(get_db)):
    """Check pending Alpaca orders for fills and record them in the local DB.

    Call ~15 min after Phase 2 to catch orders that filled after submission.
    Safe to call multiple times — already-reconciled orders are skipped.
    """
    from ..broker.alpaca import reconcile_pending_orders
    from ..services.telegram import send_telegram

    results = await reconcile_pending_orders(db)

    # Build Telegram summary
    if results:
        filled = [r for r in results if r["status"] == "filled"]
        unfilled = [r for r in results if r["status"] != "filled"]
        msg_parts = []
        if filled:
            msg_parts.append("Fills recorded:\n" + "\n".join(
                f"  {r['action'].upper()} {r['symbol']} — {r['filled_qty']}sh @ ${r['filled_price']}"
                for r in filled
            ))
        if unfilled:
            msg_parts.append("Not filled:\n" + "\n".join(
                f"  {r['action'].upper()} {r['symbol']} — {r['status']}"
                for r in unfilled
            ))
        await send_telegram("TRADEBOT // Order Reconciliation\n" + "\n".join(msg_parts))

    return {"reconciled": len(results), "results": results}


@router.post("/{recommendation_id}/reject", response_model=RejectTradeResponse, dependencies=[Depends(require_owner_pin)])
async def reject_trade(
    recommendation_id: int,
    body: RejectTradeRequest,
    db: AsyncSession = Depends(get_db),
):
    rec = (
        await db.execute(
            select(TradeRecommendation).where(TradeRecommendation.id == recommendation_id)
        )
    ).scalars().first()

    if rec is None:
        raise HTTPException(status_code=404, detail=f"Recommendation {recommendation_id} not found")
    if rec.status != "pending":
        raise HTTPException(status_code=409, detail=f"Recommendation is already {rec.status}")

    rec.status = "rejected"
    await db.commit()
    return RejectTradeResponse(recommendation_id=recommendation_id, status="rejected")
