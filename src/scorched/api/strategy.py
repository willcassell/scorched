"""Strategy API — read and write strategy.json via HTTP."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..services.strategy import load_strategy, load_strategy_json, save_strategy_json
from .deps import require_owner_pin

router = APIRouter(prefix="/strategy", tags=["strategy"])


class StrategyResponse(BaseModel):
    data: dict        # raw selections (for the dashboard form)
    prose: str        # rendered prose (for Claude / preview)
    pin_required: bool = False  # true when SETTINGS_PIN is configured


@router.get("", response_model=StrategyResponse, dependencies=[Depends(require_owner_pin)])
async def get_strategy():
    return StrategyResponse(
        data=load_strategy_json(),
        prose=load_strategy(),
        pin_required=bool(settings.settings_pin),
    )


@router.put("", response_model=StrategyResponse)
async def update_strategy(body: dict):
    # _pin is meta — pop it before writing to strategy.json so it's never persisted.
    pin = body.pop("_pin", "")
    if settings.settings_pin and pin != settings.settings_pin:
        raise HTTPException(status_code=403, detail="Incorrect PIN")
    # Merge incoming form payload into existing strategy.json rather than
    # overwriting. The dashboard form only surfaces a subset of keys; a full
    # overwrite silently wipes safety sections the form doesn't render
    # (circuit_breaker, intraday_monitor, drawdown_gate). Shallow merge is
    # correct — form fields always include all subkeys of any section they
    # own, so a top-level replace of a form-owned key is the right semantics.
    try:
        merged = load_strategy_json()
        merged.update(body)
        save_strategy_json(merged)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to write strategy file: {e}")
    return StrategyResponse(
        data=load_strategy_json(),
        prose=load_strategy(),
        pin_required=bool(settings.settings_pin),
    )
