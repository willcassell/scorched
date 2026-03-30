"""Intraday position monitoring — pure trigger check functions.

All functions are pure (no I/O) and reuse GateResult from
circuit_breaker. They check whether market or position conditions
have crossed configurable thresholds.
"""

from decimal import Decimal

from .circuit_breaker import GateResult


def check_position_drop_from_entry(
    current_price: Decimal, entry_price: Decimal, threshold_pct: float
) -> GateResult:
    """Fire if position has dropped more than threshold_pct from entry."""
    if entry_price <= 0:
        return GateResult(passed=True)
    drop_pct = float((entry_price - current_price) / entry_price * 100)
    if drop_pct > threshold_pct:
        return GateResult(
            passed=False,
            reason=f"Down {drop_pct:.1f}% from entry ${entry_price} (threshold: {threshold_pct:.1f}%)",
        )
    return GateResult(passed=True)


def check_position_drop_from_open(
    current_price: Decimal, today_open: Decimal, threshold_pct: float
) -> GateResult:
    """Fire if position has dropped more than threshold_pct from today's open."""
    if today_open <= 0:
        return GateResult(passed=True)
    drop_pct = float((today_open - current_price) / today_open * 100)
    if drop_pct > threshold_pct:
        return GateResult(
            passed=False,
            reason=f"Down {drop_pct:.1f}% from open ${today_open} (threshold: {threshold_pct:.1f}%)",
        )
    return GateResult(passed=True)


def check_spy_intraday_drop(
    spy_current: Decimal, spy_open: Decimal, threshold_pct: float
) -> GateResult:
    """Fire if SPY has dropped more than threshold_pct intraday."""
    if spy_open <= 0:
        return GateResult(passed=True)
    drop_pct = float((spy_open - spy_current) / spy_open * 100)
    if drop_pct > threshold_pct:
        return GateResult(
            passed=False,
            reason=f"SPY down {drop_pct:.1f}% intraday (threshold: {threshold_pct:.1f}%)",
        )
    return GateResult(passed=True)


def check_vix_level(vix_current: Decimal, threshold: float) -> GateResult:
    """Fire if VIX exceeds absolute threshold."""
    vix_val = float(vix_current)
    if vix_val > threshold:
        return GateResult(
            passed=False,
            reason=f"VIX at {vix_val:.1f} exceeds {threshold:.0f}",
        )
    return GateResult(passed=True)


def check_volume_surge(
    current_volume: float, avg_volume_20d: float, threshold_multiplier: float
) -> GateResult:
    """Fire if current volume exceeds threshold_multiplier times average."""
    if avg_volume_20d <= 0:
        return GateResult(passed=True)
    ratio = current_volume / avg_volume_20d
    if ratio > threshold_multiplier:
        return GateResult(
            passed=False,
            reason=f"Volume surge {ratio:.1f}x average (threshold: {threshold_multiplier:.1f}x)",
        )
    return GateResult(passed=True)


def check_market_triggers(
    spy_current: Decimal,
    spy_open: Decimal,
    vix_current: Decimal,
    config: dict,
) -> list[GateResult]:
    """Run SPY check. Returns list of FIRED results only.

    VIX is intentionally excluded as a trigger — a sustained high VIX
    causes repeated alerts for every position. VIX level is still passed
    to Claude as market_context so it informs exit decisions.
    """
    fired: list[GateResult] = []

    spy_result = check_spy_intraday_drop(
        spy_current, spy_open, config.get("spy_intraday_drop_pct", 2.0)
    )
    if not spy_result.passed:
        fired.append(spy_result)

    return fired


def check_intraday_triggers(
    current_price: Decimal,
    entry_price: Decimal,
    today_open: Decimal,
    current_volume: float,
    avg_volume_20d: float,
    market_triggers: list[GateResult],
    config: dict,
) -> list[GateResult]:
    """Run all position-level checks + include market triggers. Returns FIRED only."""
    fired: list[GateResult] = []

    entry_result = check_position_drop_from_entry(
        current_price, entry_price, config.get("position_drop_from_entry_pct", 5.0)
    )
    if not entry_result.passed:
        fired.append(entry_result)

    open_result = check_position_drop_from_open(
        current_price, today_open, config.get("position_drop_from_open_pct", 3.0)
    )
    if not open_result.passed:
        fired.append(open_result)

    vol_result = check_volume_surge(
        current_volume, avg_volume_20d, config.get("volume_surge_multiplier", 3.0)
    )
    if not vol_result.passed:
        fired.append(vol_result)

    fired.extend(market_triggers)

    return fired
