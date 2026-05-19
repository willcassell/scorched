"""Backstop cooldown math for failed intraday sells.

When the intraday endpoint surfaces a sell failure via "[SELL FAILED: ...]"
in the reasoning, the cron records a SHORTER cooldown (cooldown_minutes / 2,
floor 5 min) so the next 5-min tick doesn't re-fire and storm Alpaca with
duplicate-client_order_id rejections.

The cron's set-cooldown formula is:
    cooldowns[symbol] = time.time() - (cooldown_minutes - backstop_min) * 60

This makes `is_on_cooldown` true for `backstop_min` minutes.
"""
import sys
import time
from pathlib import Path

import pytest

# Match cron/intraday_monitor.py's sys.path setup
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cron"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from intraday_monitor import is_on_cooldown  # noqa: E402


def _set_backstop(cooldowns: dict, symbol: str, *, cooldown_minutes: int) -> int:
    """Mirror cron's backstop formula and return the backstop window."""
    backstop_min = max(5, cooldown_minutes // 2)
    cooldowns[symbol] = time.time() - (cooldown_minutes - backstop_min) * 60
    return backstop_min


def test_backstop_makes_symbol_on_cooldown_immediately():
    cooldowns: dict = {}
    _set_backstop(cooldowns, "NVDA", cooldown_minutes=30)
    assert is_on_cooldown("NVDA", cooldowns, 30) is True


def test_backstop_window_is_shorter_than_full_cooldown():
    cooldowns: dict = {}
    backstop_min = _set_backstop(cooldowns, "NVDA", cooldown_minutes=30)

    # Backstop should be 15 min for cooldown_minutes=30
    assert backstop_min == 15

    # Just past the backstop window — symbol should be off cooldown
    cooldowns["NVDA"] -= (backstop_min + 1) * 60
    assert is_on_cooldown("NVDA", cooldowns, 30) is False


def test_backstop_respects_5min_floor_when_cooldown_is_small():
    """cooldown_minutes // 2 = 4 → floor at 5."""
    cooldowns: dict = {}
    backstop_min = _set_backstop(cooldowns, "AAPL", cooldown_minutes=8)
    assert backstop_min == 5
    # With cooldown=8 and backstop=5, the symbol is on cooldown for ~5 min,
    # not the full 8 — so a tick 6 min later should be free to re-fire.
    cooldowns["AAPL"] -= (5 + 1) * 60
    assert is_on_cooldown("AAPL", cooldowns, 8) is False
