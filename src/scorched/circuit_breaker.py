"""Pre-execution circuit breaker — gates buy orders at market open.

Checks are pure functions (no I/O) so they're easy to test.
The `run_circuit_breaker` async function fetches live data and
calls the pure checkers.
"""
import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    passed: bool
    reason: str = ""


def check_stock_gate(
    symbol: str,
    suggested_price: Decimal,
    current_price: Decimal,
    prior_close: Decimal,
    config: dict,
) -> GateResult:
    """Check whether a single stock's price action disqualifies a buy."""
    if not config.get("enabled", True):
        return GateResult(passed=True)

    # Gap down from prior close
    if prior_close > 0:
        gap_pct = float((prior_close - current_price) / prior_close * 100)
        threshold = config.get("stock_gap_down_pct", 2.0)
        if gap_pct > threshold:
            return GateResult(
                passed=False,
                reason=f"{symbol} gap_down {gap_pct:.1f}% from prior close (threshold: {threshold}%)",
            )

    # Drift from Claude's suggested price
    if suggested_price > 0:
        drift_pct = float((suggested_price - current_price) / suggested_price * 100)
        threshold = config.get("stock_price_drift_pct", 1.5)
        if drift_pct > threshold:
            return GateResult(
                passed=False,
                reason=f"{symbol} drift {drift_pct:.1f}% below suggested ${suggested_price} (threshold: {threshold}%)",
            )

    return GateResult(passed=True)


def check_market_gate(
    spy_current: Decimal,
    spy_prior_close: Decimal,
    vix_current: Decimal,
    vix_prior_close: Decimal,
    config: dict,
) -> GateResult:
    """Check whether broad market conditions disqualify ALL buys."""
    if not config.get("enabled", True):
        return GateResult(passed=True)

    # SPY gap down
    if spy_prior_close > 0:
        spy_gap_pct = float((spy_prior_close - spy_current) / spy_prior_close * 100)
        threshold = config.get("spy_gap_down_pct", 1.0)
        if spy_gap_pct > threshold:
            return GateResult(
                passed=False,
                reason=f"SPY gap_down {spy_gap_pct:.1f}% (threshold: {threshold}%)",
            )

    # VIX absolute level
    vix_max = config.get("vix_absolute_max", 30)
    if float(vix_current) > vix_max:
        return GateResult(
            passed=False,
            reason=f"VIX at {float(vix_current):.1f} exceeds max {vix_max}",
        )

    # VIX overnight spike
    if vix_prior_close > 0:
        vix_spike_pct = float((vix_current - vix_prior_close) / vix_prior_close * 100)
        threshold = config.get("vix_spike_pct", 20.0)
        if vix_spike_pct > threshold:
            return GateResult(
                passed=False,
                reason=f"VIX spiked {vix_spike_pct:.1f}% overnight (threshold: {threshold}%)",
            )

    return GateResult(passed=True)


def check_gap_up_gate(
    symbol: str,
    current_price: Decimal,
    prior_close: Decimal,
    config: dict,
) -> GateResult:
    """Check whether a stock has gapped up excessively (potential chase risk)."""
    if not config.get("enabled", True):
        return GateResult(passed=True)
    if prior_close > 0:
        gap_up_pct = float((current_price - prior_close) / prior_close * 100)
        threshold = config.get("stock_gap_up_pct", 5.0)
        if gap_up_pct > threshold:
            return GateResult(
                passed=False,
                reason=f"{symbol} gap_up {gap_up_pct:.1f}% from prior close (threshold: {threshold}%) — chase risk",
            )
    return GateResult(passed=True)


async def fetch_gate_data(symbols: list[str]) -> dict:
    """Fetch live prices for circuit breaker checks via yfinance.

    Returns dict with keys: spy_current, spy_prior_close, vix_current,
    vix_prior_close, and per-symbol current_price and prior_close.
    """
    import yfinance as yf

    all_symbols = list(set(symbols + ["SPY", "^VIX"]))

    def _fetch():
        data = {}
        for sym in all_symbols:
            try:
                ticker = yf.Ticker(sym)
                hist = ticker.history(period="5d")
                if len(hist) >= 2:
                    data[sym] = {
                        "current": Decimal(str(hist["Close"].iloc[-1])),
                        "prior_close": Decimal(str(hist["Close"].iloc[-2])),
                    }
                elif len(hist) == 1:
                    price = Decimal(str(hist["Close"].iloc[-1]))
                    data[sym] = {"current": price, "prior_close": price}
            except Exception as e:
                logger.warning("Circuit breaker: failed to fetch %s: %s", sym, e)
        return data

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch)


async def run_circuit_breaker(
    recommendations: list[dict],
    config: dict,
) -> list[dict]:
    """Run all gate checks against pending buy recommendations.

    Returns the input list with a `gate_result` key added to each dict.
    Sell recommendations are always passed through.
    """
    if not config.get("enabled", True):
        for rec in recommendations:
            rec["gate_result"] = GateResult(passed=True)
        return recommendations

    buy_symbols = [r["symbol"] for r in recommendations if r["action"] == "buy"]

    if not buy_symbols:
        for rec in recommendations:
            rec["gate_result"] = GateResult(passed=True)
        return recommendations

    data = await fetch_gate_data(buy_symbols)

    # Market-level gate
    spy_data = data.get("SPY", {})
    vix_data = data.get("^VIX", {})
    market_gate = check_market_gate(
        spy_current=spy_data.get("current", Decimal("0")),
        spy_prior_close=spy_data.get("prior_close", Decimal("0")),
        vix_current=vix_data.get("current", Decimal("0")),
        vix_prior_close=vix_data.get("prior_close", Decimal("0")),
        config=config,
    )

    for rec in recommendations:
        if rec["action"] == "sell":
            rec["gate_result"] = GateResult(passed=True)
            continue

        if not market_gate.passed:
            rec["gate_result"] = market_gate
            continue

        sym_data = data.get(rec["symbol"], {})
        rec["gate_result"] = check_stock_gate(
            symbol=rec["symbol"],
            suggested_price=Decimal(str(rec.get("suggested_price", 0))),
            current_price=sym_data.get("current", Decimal("0")),
            prior_close=sym_data.get("prior_close", Decimal("0")),
            config=config,
        )

    return recommendations
