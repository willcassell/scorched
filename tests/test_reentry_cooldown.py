"""Tests for Task 9: re-entry cooldown gate (code-enforced).

`check_reentry_cooldown()` blocks a BUY if `trade_history` has a SELL for the
symbol within the last `cooldown_days` NYSE trading days — the whipsaw guard
(e.g. CVX sold 6/11, re-bought 6/15 cost real money; Experiment B deferred
this deliberately).

Trading-day math uses `pandas_market_calendars` (same library/pattern as
`_is_market_open`), not calendar days, so a sell just before a weekend or
holiday cluster isn't miscounted as "long enough ago" just because several
calendar days elapsed.

All fixed dates below are anchored to FIXED_TODAY = 2026-06-22 (a Monday).
The preceding week has a real NYSE gap: Thursday 2026-06-18 was the last
session before Friday 2026-06-19 (Juneteenth holiday) + the weekend, so
2026-06-18 -> 2026-06-22 is a 4-calendar-day gap but only 1 NYSE trading day.
That gap is exactly what makes the "weekend gap counted in trading days"
case meaningful: a naive calendar-day implementation would allow it (4 >= 3),
the correct trading-day implementation blocks it (1 < 3).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from scorched.models import TradeHistory
from scorched.services import recommender as rec_module
from scorched.services.recommender import check_reentry_cooldown

FIXED_TODAY = date(2026, 6, 22)

# NYSE sessions in the 20 calendar days before/including FIXED_TODAY, verified
# directly against pandas_market_calendars.get_calendar("NYSE"):
#   ..., 2026-06-15, 2026-06-16, 2026-06-17, 2026-06-18, [gap: 6/19 holiday, 6/20-21 weekend], 2026-06-22
DAYS_BACK_1 = date(2026, 6, 18)  # 1 NYSE trading day before FIXED_TODAY
DAYS_BACK_3 = date(2026, 6, 16)  # exactly 3 NYSE trading days before FIXED_TODAY
DAYS_BACK_5 = date(2026, 6, 11)  # 5 NYSE trading days before FIXED_TODAY


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    """Pin market_today() as seen by recommender.py so trading-day math is
    deterministic and doesn't depend on the real calendar date at test time."""
    monkeypatch.setattr(rec_module, "market_today", lambda: FIXED_TODAY)


async def _seed_sell(db_session, symbol: str, executed_at: datetime) -> None:
    db_session.add(
        TradeHistory(
            symbol=symbol,
            action="sell",
            shares=Decimal("10"),
            execution_price=Decimal("100.00"),
            total_value=Decimal("1000.00"),
            executed_at=executed_at,
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_sell_one_trading_day_ago_blocks(db_session):
    await _seed_sell(db_session, "CVX", datetime.combine(DAYS_BACK_1, datetime.min.time()))
    allowed, reason = await check_reentry_cooldown(db_session, "CVX", cooldown_days=3)
    assert allowed is False
    assert reason is not None


@pytest.mark.asyncio
async def test_sell_exactly_three_trading_days_ago_allowed(db_session):
    """Boundary case: strictly-greater-than passes. A sell exactly
    `cooldown_days` trading days ago is old enough to re-enter."""
    await _seed_sell(db_session, "CVX", datetime.combine(DAYS_BACK_3, datetime.min.time()))
    allowed, reason = await check_reentry_cooldown(db_session, "CVX", cooldown_days=3)
    assert allowed is True
    assert reason is None


@pytest.mark.asyncio
async def test_sell_five_trading_days_ago_allowed_with_three_day_cooldown(db_session):
    await _seed_sell(db_session, "CVX", datetime.combine(DAYS_BACK_5, datetime.min.time()))
    allowed, reason = await check_reentry_cooldown(db_session, "CVX", cooldown_days=3)
    assert allowed is True
    assert reason is None


@pytest.mark.asyncio
async def test_no_sells_allowed(db_session):
    allowed, reason = await check_reentry_cooldown(db_session, "CVX", cooldown_days=3)
    assert allowed is True
    assert reason is None


@pytest.mark.asyncio
async def test_weekend_and_holiday_gap_counted_in_trading_days_not_calendar_days(db_session):
    """2026-06-18 -> 2026-06-22 is a 4-calendar-day gap (Juneteenth holiday +
    weekend) but only 1 NYSE trading day. A naive calendar-day cooldown check
    would incorrectly allow this (4 >= 3); the correct trading-day count must
    still block it (1 < 3)."""
    await _seed_sell(db_session, "CVX", datetime.combine(DAYS_BACK_1, datetime.min.time()))
    allowed, reason = await check_reentry_cooldown(db_session, "CVX", cooldown_days=3)
    assert allowed is False
    assert reason is not None


@pytest.mark.asyncio
async def test_sell_today_blocks(db_session):
    await _seed_sell(db_session, "CVX", datetime.combine(FIXED_TODAY, datetime.min.time()))
    allowed, reason = await check_reentry_cooldown(db_session, "CVX", cooldown_days=3)
    assert allowed is False


@pytest.mark.asyncio
async def test_sell_of_different_symbol_does_not_block(db_session):
    await _seed_sell(db_session, "XOM", datetime.combine(DAYS_BACK_1, datetime.min.time()))
    allowed, reason = await check_reentry_cooldown(db_session, "CVX", cooldown_days=3)
    assert allowed is True
    assert reason is None


@pytest.mark.asyncio
async def test_cooldown_days_zero_or_negative_always_allowed(db_session):
    await _seed_sell(db_session, "CVX", datetime.combine(FIXED_TODAY, datetime.min.time()))
    allowed, reason = await check_reentry_cooldown(db_session, "CVX", cooldown_days=0)
    assert allowed is True


@pytest.mark.asyncio
async def test_db_error_fails_open_and_logs(db_session, monkeypatch, caplog):
    """Fail-open ONLY on DB error — telemetry/gate loss must never block a
    trade that would otherwise be fine (project convention: gates are
    best-effort, never raise into the hot path)."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("db connection lost")

    monkeypatch.setattr(db_session, "execute", _boom)
    with caplog.at_level("ERROR"):
        allowed, reason = await check_reentry_cooldown(db_session, "CVX", cooldown_days=3)
    assert allowed is True
    assert any("reentry_cooldown" in r.message for r in caplog.records)


def test_gate_is_wired_into_the_buy_chain_after_factor_alignment():
    """Regression guard: the gate-instrumentation tests exercise
    check_reentry_cooldown + record_gate_decision directly (matching how the
    sibling gates are tested), which would still pass even if the call site
    in generate_recommendations() were deleted. Grep-assert it's actually
    wired, in the right position in the chain (per the brief: after
    factor_alignment)."""
    src = (
        Path(__file__).resolve().parent.parent
        / "src/scorched/services/recommender.py"
    ).read_text()
    assert 'gate="reentry_cooldown"' in src
    assert 'await check_reentry_cooldown(' in src
    assert src.index('gate="factor_alignment"') < src.index('gate="reentry_cooldown"')
