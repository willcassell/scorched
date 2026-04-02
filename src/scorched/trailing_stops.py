"""ATR-based trailing stop logic — pure functions, no I/O.

The trailing stop ratchets up as the position gains.  It never moves down.
When ATR data is available the stop is placed at
``high_water_mark - atr * atr_multiplier``; a fixed-percentage floor
(default -5 %) guarantees a minimum distance from entry.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def compute_trailing_stop(
    entry_price: Decimal,
    current_price: Decimal,
    high_water_mark: Decimal | None,
    atr: float | None,
    atr_multiplier: float = 2.0,
    min_stop_pct: float = 5.0,
    previous_stop: Decimal | None = None,
) -> dict:
    """Compute the trailing stop price based on ATR and high-water mark.

    Parameters
    ----------
    entry_price : Decimal
        Original entry (avg cost basis) for the position.
    current_price : Decimal
        Latest market price.
    high_water_mark : Decimal | None
        Previous high-water mark, or ``None`` for a brand-new position.
    atr : float | None
        Average True Range (14-period) in dollar terms.  ``None`` when
        ATR data is unavailable; falls back to fixed-pct stop only.
    atr_multiplier : float
        How many ATRs below the high-water mark to set the stop (default 2).
    min_stop_pct : float
        Minimum stop distance as a percentage of entry price (default 5 %).
        Acts as a floor — the stop is never looser than this.
    previous_stop : Decimal | None
        The last recorded trailing stop price.  The new stop will never be
        lower than this value (ratchet-up guarantee).

    Returns
    -------
    dict with keys:
        trailing_stop_price : Decimal
        high_water_mark : Decimal
        stop_type : str   ("atr" | "fixed_pct")
        distance_pct : float  (current price distance from stop, positive = safe)
    """
    _q4 = Decimal("0.0001")

    # --- High-water mark: ratchet up, never down --------------------------
    if high_water_mark is None:
        new_hwm = max(entry_price, current_price)
    else:
        new_hwm = max(high_water_mark, current_price)

    # --- Fixed-percentage stop (floor) ------------------------------------
    fixed_stop = (entry_price * (Decimal("1") - Decimal(str(min_stop_pct)) / Decimal("100"))).quantize(_q4, ROUND_HALF_UP)

    # --- ATR-based stop ---------------------------------------------------
    stop_type = "fixed_pct"
    if atr is not None and atr > 0:
        atr_dec = Decimal(str(atr))
        atr_stop = (new_hwm - atr_dec * Decimal(str(atr_multiplier))).quantize(_q4, ROUND_HALF_UP)
        # Use whichever is HIGHER (tighter protection)
        if atr_stop > fixed_stop:
            candidate = atr_stop
            stop_type = "atr"
        else:
            candidate = fixed_stop
    else:
        candidate = fixed_stop

    # --- Ratchet: stop never moves down -----------------------------------
    if previous_stop is not None and previous_stop > candidate:
        candidate = previous_stop

    # --- Distance from current price --------------------------------------
    if current_price > 0:
        distance_pct = float(
            (current_price - candidate) / current_price * Decimal("100")
        )
    else:
        distance_pct = 0.0

    return {
        "trailing_stop_price": candidate,
        "high_water_mark": new_hwm,
        "stop_type": stop_type,
        "distance_pct": round(distance_pct, 2),
    }
