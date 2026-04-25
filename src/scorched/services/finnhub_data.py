"""Finnhub analyst consensus and price target data."""
from __future__ import annotations

import logging
import time
import requests

from ..http_retry import retry_call
from ..config import settings
from contextlib import nullcontext
from typing import Any

logger = logging.getLogger(__name__)


def _api_ctx(tracker, service, endpoint, symbol=None):
    """Return a track_call context if tracker is provided, else nullcontext."""
    if tracker is None:
        return nullcontext()
    from ..api_tracker import track_call
    return track_call(tracker, service, endpoint, symbol=symbol)


def _get_val(obj: Any, *names: str) -> Any:
    """Get a value from an object that may be a dict or an SDK object.

    Finnhub returns plain dicts in production but tests use MagicMock objects.
    Tries dict-style access first, then attribute access with fallback names.
    """
    for name in names:
        # Dict access (production Finnhub responses)
        if isinstance(obj, dict) and name in obj:
            return obj[name]
    for name in names:
        # Attribute access (MagicMock in tests, or SDK objects)
        if hasattr(type(obj), name) or name in getattr(obj, "__dict__", {}):
            return getattr(obj, name)
    # Final fallback: try getattr directly
    for name in names:
        try:
            val = getattr(obj, name)
            if not callable(val) or isinstance(val, (int, float, str)):
                return val
        except AttributeError:
            continue
    return 0


def fetch_analyst_consensus_sync(
    symbols: list[str], client: Any | None, tracker=None
) -> dict[str, dict[str, Any]]:
    """Fetch analyst recommendation trends and price targets for each symbol.

    Args:
        symbols: List of ticker symbols.
        client: A finnhub.Client instance, or None (returns empty dict).

    Returns:
        Mapping of symbol -> dict with consensus counts and price targets.
    """
    if client is None:
        return {}

    results: dict[str, dict[str, Any]] = {}

    for symbol in symbols:
        try:
            data: dict[str, Any] = {}

            # Recommendation trends (1 call/symbol)
            # Note: price_target endpoint requires paid Finnhub plan (403 on free tier)
            try:
                with _api_ctx(tracker, "finnhub", "recommendation_trends", symbol):
                    trends = retry_call(
                        client.recommendation_trends, symbol,
                        label=f"Finnhub {symbol}",
                    )
                if trends:
                    latest = trends[0]
                    data["strong_buy"] = _get_val(latest, "strong_buy", "strongBuy")
                    data["buy"] = _get_val(latest, "buy")
                    data["hold"] = _get_val(latest, "hold")
                    data["sell"] = _get_val(latest, "sell")
                    data["strong_sell"] = _get_val(latest, "strong_sell", "strongSell")
            except Exception as exc:
                logger.warning("Finnhub recommendation_trends failed for %s: %s", symbol, exc)

            if data:
                results[symbol] = data

            # Rate limit: Finnhub free tier = 60 calls/min; 1 call/symbol
            time.sleep(1.1)

        except Exception as exc:
            logger.warning("Finnhub fetch failed for %s: %s", symbol, exc)

    return results



def build_analyst_context(analyst_data: dict[str, dict[str, Any]]) -> str:
    """Format analyst consensus data as text for Claude's prompt.

    Args:
        analyst_data: Output from fetch_analyst_consensus_sync.

    Returns:
        Formatted text block, or empty string if no data.
    """
    if not analyst_data:
        return ""

    lines: list[str] = ["## Analyst Consensus"]

    for symbol, info in analyst_data.items():
        total = sum(
            info.get(k, 0) or 0
            for k in ("strong_buy", "buy", "hold", "sell", "strong_sell")
        )
        bullish = sum(info.get(k, 0) or 0 for k in ("strong_buy", "buy"))
        bullish_pct = (bullish / total * 100) if total > 0 else 0

        lines.append(f"\n### {symbol}")
        lines.append(
            f"  Strong Buy: {info.get('strong_buy', 0)} | "
            f"Buy: {info.get('buy', 0)} | "
            f"Hold: {info.get('hold', 0)} | "
            f"Sell: {info.get('sell', 0)} | "
            f"Strong Sell: {info.get('strong_sell', 0)}"
        )
        lines.append(f"  Bullish: {bullish_pct:.0f}%")

        target_mean = info.get("target_mean")
        target_high = info.get("target_high")
        target_low = info.get("target_low")
        if target_mean is not None:
            parts = [f"Mean: ${target_mean:.2f}"]
            if target_low is not None:
                parts.append(f"Low: ${target_low:.2f}")
            if target_high is not None:
                parts.append(f"High: ${target_high:.2f}")
            lines.append(f"  Price Targets — {' | '.join(parts)}")

    return "\n".join(lines)


def fetch_sector_for_symbol(symbol: str) -> str | None:
    """Fetch GICS sector from Finnhub stock/profile2 endpoint. Returns None on failure.

    Used as fallback when the static `_SECTOR_ETF_MAP` has no entry for `symbol`.
    Finnhub's `finnhubIndustry` field is GICS-aligned for the major sectors.

    NOTE (Tier 2 concern): Finnhub returns names like "Information Technology" or
    "Financial Services" whereas `_ETF_TO_SECTOR` uses "Technology" and "Financials".
    If sector-name normalization is needed, it should be added as a Tier 2 follow-up
    in `_get_sector_for_symbol` — not here. The gate still fails closed on unknown
    sectors, so a Finnhub-vs-ETF-map mismatch is conservative, not permissive.
    """
    if not settings.finnhub_api_key:
        return None
    url = "https://finnhub.io/api/v1/stock/profile2"
    params = {"symbol": symbol.upper(), "token": settings.finnhub_api_key}
    try:
        response = retry_call(lambda: requests.get(url, params=params, timeout=10))
        if response is None or response.status_code != 200:
            return None
        data = response.json()
        industry = data.get("finnhubIndustry")
        return industry if industry else None
    except Exception as exc:
        logger.warning("Finnhub sector lookup failed for %s: %s", symbol, exc)
        return None
