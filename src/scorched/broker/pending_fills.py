"""Crash recovery: track pending fills between Alpaca confirmation and DB commit.

A fill is written to the JSON file AFTER Alpaca confirms the order but BEFORE
the local DB is updated.  If the process crashes between those two steps, the
startup reconciliation in main.py will replay the DB recording.

Uses atomic writes (tempfile + os.rename) to avoid partial/corrupt JSON.
"""
import json
import logging
import os
import tempfile
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger(__name__)

PENDING_FILLS_PATH = Path("/app/logs/pending_fills.json")


def _read_fills() -> list[dict]:
    """Read the pending fills file.  Returns [] if missing or corrupt."""
    if not PENDING_FILLS_PATH.exists():
        return []
    try:
        data = json.loads(PENDING_FILLS_PATH.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read pending fills file: %s", exc)
        return []


def _write_fills(fills: list[dict]) -> None:
    """Atomically write fills list to disk (tempfile + rename)."""
    PENDING_FILLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(PENDING_FILLS_PATH.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(fills, f, indent=2, default=str)
        os.rename(tmp_path, str(PENDING_FILLS_PATH))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_pending_fill(
    order_id: str,
    symbol: str,
    action: str,
    qty: Decimal,
    fill_price: Decimal,
    recommendation_id: int | None,
) -> None:
    """Append a pending fill record (call BEFORE DB recording)."""
    fills = _read_fills()
    fills.append({
        "order_id": order_id,
        "symbol": symbol,
        "action": action,
        "qty": str(qty),
        "fill_price": str(fill_price),
        "recommendation_id": recommendation_id,
    })
    _write_fills(fills)
    logger.info("Wrote pending fill: order=%s %s %s x%s @ %s", order_id, action, symbol, qty, fill_price)


def remove_pending_fill(order_id: str) -> None:
    """Remove a fill after successful DB recording."""
    fills = _read_fills()
    new_fills = [f for f in fills if f.get("order_id") != order_id]
    if len(new_fills) < len(fills):
        _write_fills(new_fills)
        logger.info("Removed pending fill: order=%s", order_id)
    else:
        logger.debug("Pending fill not found for removal: order=%s", order_id)


def get_pending_fills() -> list[dict]:
    """Return all pending fills (used by startup reconciliation)."""
    return _read_fills()
