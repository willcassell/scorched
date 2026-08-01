"""Phase 1 gate-instrumentation tests.

Each candidate-filter gate inside `recommender.py` must persist a decision
when it rejects, so the operator can attribute "which gate killed the most
buys" without parsing logs. We test the gates as pure-ish helpers by calling
the same shared `record_gate_decision` recorder and verifying the row.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from scorched.models import GateDecision, RecommendationSession
from scorched.risk_gates import (
    check_cash_floor,
    check_holdings_cap,
    check_position_cap,
)
from scorched.services import gate_decisions as gd


@pytest.fixture
def _patch_recorder_session(db_session, monkeypatch):
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(gd, "AsyncSessionLocal", factory)
    yield factory


async def _seed_session(db_session) -> RecommendationSession:
    s = RecommendationSession(session_date=date.today())
    db_session.add(s)
    await db_session.commit()
    return s


@pytest.mark.asyncio
async def test_drawdown_block_records_decision_per_buy(db_session, _patch_recorder_session):
    s = await _seed_session(db_session)

    # Simulate the drawdown-blocked path.
    buys = ["AAPL", "MSFT", "NVDA"]
    for sym in buys:
        await gd.record_gate_decision(
            db_session,
            session_id=s.id,
            symbol=sym,
            action="buy",
            phase=gd.PHASE_FILTER,
            gate="drawdown",
            passed=False,
            reason="portfolio drawdown 9.2% exceeds threshold 8.0%",
            details={"current_drawdown_pct": 9.2, "threshold_pct": 8.0},
        )

    rows = (
        await db_session.execute(
            select(GateDecision).where(GateDecision.gate == "drawdown")
        )
    ).scalars().all()
    assert len(rows) == 3
    assert all(r.passed is False for r in rows)
    assert all(r.session_id == s.id for r in rows)
    assert all(r.recommendation_id is None for r in rows)
    assert all(r.phase == gd.PHASE_FILTER for r in rows)


@pytest.mark.asyncio
async def test_cash_floor_blocked_records_projected_and_floor(db_session, _patch_recorder_session):
    s = await _seed_session(db_session)
    cash_check = check_cash_floor(
        current_cash=Decimal("500"),
        total_portfolio_value=Decimal("1000"),
        buy_notional=Decimal("150"),
        reserve_pct=Decimal("0.10"),
    )
    assert cash_check.passed is True

    # Now force a fail: tiny cash, big buy
    cash_fail = check_cash_floor(
        current_cash=Decimal("100"),
        total_portfolio_value=Decimal("1000"),
        buy_notional=Decimal("95"),
        reserve_pct=Decimal("0.10"),
    )
    assert cash_fail.passed is False

    await gd.record_gate_decision(
        db_session,
        session_id=s.id,
        symbol="AAPL",
        action="buy",
        phase=gd.PHASE_FILTER,
        gate="cash_floor",
        passed=cash_fail.passed,
        reason=cash_fail.reason,
        details={
            "running_cash": Decimal("100"),
            "buy_notional": Decimal("95"),
            "projected_cash": cash_fail.projected_cash,
            "floor": cash_fail.floor,
            "reserve_pct": 0.10,
        },
    )
    row = (await db_session.execute(
        select(GateDecision).where(GateDecision.gate == "cash_floor")
    )).scalars().first()
    assert row is not None
    assert row.passed is False
    assert row.details is not None
    assert float(row.details["floor"]) == pytest.approx(100.0)
    assert float(row.details["projected_cash"]) == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_position_cap_records_projected_pct(db_session, _patch_recorder_session):
    s = await _seed_session(db_session)
    pc = check_position_cap(
        existing_market_value=Decimal("0"),
        buy_notional=Decimal("400"),  # 40% of 1000
        total_portfolio_value=Decimal("1000"),
        max_position_pct=Decimal("33"),
    )
    assert pc.passed is False
    await gd.record_gate_decision(
        db_session,
        session_id=s.id,
        symbol="NVDA",
        action="buy",
        phase=gd.PHASE_FILTER,
        gate="position_cap",
        passed=pc.passed,
        reason=pc.reason,
        details={"projected_pct": pc.projected_pct, "cap_pct": pc.cap_pct},
    )
    row = (await db_session.execute(
        select(GateDecision).where(GateDecision.gate == "position_cap")
    )).scalars().first()
    assert row is not None
    assert row.passed is False
    assert row.details["cap_pct"] == 33.0


@pytest.mark.asyncio
async def test_holdings_cap_records_projected_count(db_session, _patch_recorder_session):
    s = await _seed_session(db_session)
    hc = check_holdings_cap(
        held_symbols={"A", "B", "C", "D", "E"},
        accepted_new_symbols=set(),
        proposed_symbol="NEW",
        max_holdings=5,
    )
    assert hc.passed is False
    await gd.record_gate_decision(
        db_session,
        session_id=s.id,
        symbol="NEW",
        action="buy",
        phase=gd.PHASE_FILTER,
        gate="holdings_cap",
        passed=hc.passed,
        reason=hc.reason,
        details={"projected_count": hc.projected_count, "cap": hc.cap},
    )
    row = (await db_session.execute(
        select(GateDecision).where(GateDecision.gate == "holdings_cap")
    )).scalars().first()
    assert row is not None
    assert row.details["projected_count"] == 6
    assert row.details["cap"] == 5


@pytest.mark.asyncio
async def test_risk_review_records_approve_and_reject_separately(
    db_session, _patch_recorder_session
):
    """Both verdicts must be persisted so the funnel ratio is queryable."""
    s = await _seed_session(db_session)
    decisions = [
        ("AAPL", "approve", None),
        ("MSFT", "reject", "macro headwind, defer"),
        ("NVDA", "approve", None),
    ]
    for sym, verdict, reason in decisions:
        await gd.record_gate_decision(
            db_session,
            session_id=s.id,
            symbol=sym,
            action="buy",
            phase=gd.PHASE_RISK_REVIEW,
            gate="risk_review",
            passed=(verdict != "reject"),
            reason=reason if verdict == "reject" else None,
            details={"verdict": verdict},
        )

    rows = (await db_session.execute(
        select(GateDecision).where(GateDecision.gate == "risk_review")
    )).scalars().all()
    assert len(rows) == 3
    by_symbol = {r.symbol: r for r in rows}
    assert by_symbol["AAPL"].passed is True
    assert by_symbol["MSFT"].passed is False
    assert by_symbol["MSFT"].reason == "macro headwind, defer"
    assert by_symbol["NVDA"].passed is True

    summary = await gd.summarize_gate_attribution(db_session, days=14)
    risk_row = next(r for r in summary if r.gate == "risk_review")
    assert risk_row.passed_count == 2
    assert risk_row.blocked_count == 1


@pytest.mark.asyncio
async def test_exposure_check_records_decision_every_session(db_session):
    """Task 8: generate_recommendations() writes an `exposure_check` row every
    session, regardless of verdict — this is portfolio-level telemetry, not a
    per-buy gate, so it uses the PORTFOLIO/none sentinels (there is no single
    symbol or buy/sell action to attach it to).

    Matches the real call site (recommender.py), which passes
    use_caller_session=True and session_id=session_row.id from the same
    flushed-but-uncommitted transaction — not the patched-AsyncSessionLocal
    pattern used by the other gates in this file (those go through the
    default own-session path).
    """
    s = await _seed_session(db_session)

    await gd.record_gate_decision(
        db_session,
        use_caller_session=True,
        session_id=s.id,
        symbol="PORTFOLIO",
        action="none",
        phase=gd.PHASE_FILTER,
        gate="exposure_check",
        passed=False,
        reason="underinvested",
        details={
            "invested_pct": 11.9,
            "target_min": 60.0,
            "target_max": 90.0,
            "spy_above_20dma": True,
            "drawdown_gate_active": False,
        },
    )

    row = (await db_session.execute(
        select(GateDecision).where(GateDecision.gate == "exposure_check")
    )).scalars().first()
    assert row is not None
    assert row.symbol == "PORTFOLIO"
    assert row.action == "none"
    assert row.passed is False
    assert row.reason == "underinvested"
    assert row.session_id == s.id
    assert row.details["invested_pct"] == pytest.approx(11.9)
    assert row.details["target_min"] == 60.0
    assert row.details["target_max"] == 90.0
    assert row.details["spy_above_20dma"] is True
    assert row.details["drawdown_gate_active"] is False


@pytest.mark.asyncio
async def test_exposure_check_passed_when_in_range(db_session):
    """A healthy session (in_range/defensive_ok) still writes a row —
    passed=True — so the audit trail has a positive record, not just
    failures."""
    s = await _seed_session(db_session)

    await gd.record_gate_decision(
        db_session,
        use_caller_session=True,
        session_id=s.id,
        symbol="PORTFOLIO",
        action="none",
        phase=gd.PHASE_FILTER,
        gate="exposure_check",
        passed=True,
        reason="in_range",
        details={
            "invested_pct": 70.0,
            "target_min": 60.0,
            "target_max": 90.0,
            "spy_above_20dma": True,
            "drawdown_gate_active": False,
        },
    )

    row = (await db_session.execute(
        select(GateDecision).where(GateDecision.gate == "exposure_check")
    )).scalars().first()
    assert row is not None
    assert row.passed is True
    assert row.reason == "in_range"
