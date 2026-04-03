"""Timezone-aware date/time helpers for the trading day."""

from datetime import date, datetime
from zoneinfo import ZoneInfo
import os

MARKET_TZ = ZoneInfo(os.environ.get("MARKET_TIMEZONE", "America/New_York"))


def market_today() -> date:
    """Return today's date in the market timezone."""
    return datetime.now(MARKET_TZ).date()


def market_now() -> datetime:
    """Return the current timezone-aware datetime in the market timezone."""
    return datetime.now(MARKET_TZ)
