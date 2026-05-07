"""Tests for the gate-decision recorder + summary helpers.

Verifies the operator can answer 'which gate killed the most buys this week?'
without parsing logs — the question Codex's adversarial review asked us to
answer before tuning prompts.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from scorched.api.deps import require_owner_pin
from scorched.database import get_db
from scorched.main import app
from scorched.models import GateDecision, RecommendationSession, TradeRecommendation
from scorched.services import gate_decisions as gd


@pytest.fixture
def _override_db(db_session):
    async def _get_db_override():
        yield db_session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[require_owner_pin] = lambda: None
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def _patch_recorder_session(db_session, monkeypatch):
    """Point gate_decisions.AsyncSessionLocal at the test in-memory SQLite engine."""
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(gd, "AsyncSessionLocal", factory)
    yield factory


@pytest.mark.asyncio
async def test_recorder_persists_even_when_caller_session_rolls_back(
    db_session, _patch_recorder_session
):
    """Hot-path rollback must not discard the gate decision.

    A gate that REJECTS raises ValueError; the surrounding transaction rolls
    back. The recorder uses its own session so the decision persists for
    operator forensics regardless.
    """
    db_session.add(RecommendationSession(session_date=datetime.utcnow().date()))
    await db_session.flush()

    # Pretend the caller is mid-transaction with uncommitted state...
    db_session.add(RecommendationSession(session_date=datetime.utcnow().date() - timedelta(days=1)))
    # ... and write a gate decision while uncommitted.
    await gd.record_gate_decision(
        db_session,
        symbol="AAPL",
        action="buy",
        phase=gd.PHASE_CONFIRM,
        gate="risk_gates",
        passed=False,
        reason="cash_floor: would breach",
        details={"projected_cash": Decimal("90.00"), "floor": Decimal("100.00")},
    )
    # Now roll back the caller transaction; the gate decision must remain.
    await db_session.rollback()

    rows = (await db_session.execute(select(GateDecision))).scalars().all()
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    assert rows[0].passed is False
    assert rows[0].reason == "cash_floor: would breach"
    assert rows[0].details == {"projected_cash": 90.0, "floor": 100.0}


@pytest.mark.asyncio
async def test_recorder_swallows_serializer_failure(db_session, _patch_recorder_session, caplog):
    """Non-serializable details must log + swallow, not raise into the hot path."""
    class Unserializable:
        pass

    await gd.record_gate_decision(
        db_session,
        symbol="MSFT",
        action="buy",
        phase=gd.PHASE_CONFIRM,
        gate="drift",
        passed=True,
        details={"obj": Unserializable()},
    )
    rows = (await db_session.execute(select(GateDecision))).scalars().all()
    assert rows == []  # nothing persisted
    assert any("Failed to record gate decision" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_summarize_groups_by_phase_and_gate(db_session, _patch_recorder_session):
    """Summary must collapse repeated identical decisions into counts."""
    decisions = [
        ("AAPL", gd.PHASE_CONFIRM, "drift", True, None),
        ("MSFT", gd.PHASE_CONFIRM, "drift", False, "drift 8% > 5% tolerance"),
        ("NVDA", gd.PHASE_CONFIRM, "risk_gates", False, "cash_floor: would breach"),
        ("META", gd.PHASE_CONFIRM, "risk_gates", False, "holdings: cap exceeded"),
        ("AMD", gd.PHASE_CONFIRM, "circuit_breaker", False, "VIX data unavailable"),
    ]
    for symbol, phase, gate, passed, reason in decisions:
        await gd.record_gate_decision(
            db_session, symbol=symbol, action="buy", phase=phase, gate=gate,
            passed=passed, reason=reason,
        )

    rows = await gd.summarize_gate_attribution(db_session, days=14)
    by_key = {(r.phase, r.gate): r for r in rows}

    assert by_key[(gd.PHASE_CONFIRM, "drift")].blocked_count == 1
    assert by_key[(gd.PHASE_CONFIRM, "drift")].passed_count == 1
    assert by_key[(gd.PHASE_CONFIRM, "risk_gates")].blocked_count == 2
    assert "cash_floor: would breach" in by_key[(gd.PHASE_CONFIRM, "risk_gates")].sample_reasons
    assert "holdings: cap exceeded" in by_key[(gd.PHASE_CONFIRM, "risk_gates")].sample_reasons
    # Summary is sorted by blocked_count desc — risk_gates (2 blocked) before
    # drift / circuit_breaker (1 each).
    assert rows[0].gate == "risk_gates"


@pytest.mark.asyncio
async def test_summary_lookback_excludes_old_records(db_session, _patch_recorder_session):
    """Records older than `days` must not appear in the summary."""
    old = GateDecision(
        symbol="OLD", action="buy", phase=gd.PHASE_CONFIRM, gate="risk_gates",
        passed=False, reason="should be excluded",
        created_at=datetime.utcnow() - timedelta(days=30),
    )
    db_session.add(old)
    await db_session.commit()

    await gd.record_gate_decision(
        db_session, symbol="NEW", action="buy", phase=gd.PHASE_CONFIRM, gate="risk_gates",
        passed=False, reason="recent",
    )

    rows = await gd.summarize_gate_attribution(db_session, days=14)
    flat_reasons: list[str] = []
    for r in rows:
        flat_reasons.extend(r.sample_reasons)
    assert "should be excluded" not in flat_reasons
    assert "recent" in flat_reasons


@pytest.mark.asyncio
async def test_gate_attribution_endpoint_returns_summary(
    db_session, _override_db, _patch_recorder_session
):
    await gd.record_gate_decision(
        db_session, symbol="AAPL", action="buy", phase=gd.PHASE_CONFIRM,
        gate="risk_gates", passed=False, reason="cash_floor: would breach",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/api/v1/system/gate-attribution?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["lookback_days"] == 7
    assert any(row["gate"] == "risk_gates" and row["blocked_count"] == 1 for row in body["rows"])


@pytest.mark.asyncio
async def test_gate_decisions_endpoint_filters_by_only_blocked(
    db_session, _override_db, _patch_recorder_session
):
    await gd.record_gate_decision(
        db_session, symbol="AAPL", action="buy", phase=gd.PHASE_CONFIRM,
        gate="drift", passed=True,
    )
    await gd.record_gate_decision(
        db_session, symbol="AAPL", action="buy", phase=gd.PHASE_CONFIRM,
        gate="risk_gates", passed=False, reason="position_cap: exposure too high",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/api/v1/system/gate-decisions?only_blocked=true")
    assert r.status_code == 200
    decisions = r.json()["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["passed"] is False
    assert "position_cap" in decisions[0]["reason"]


# ---------------------------------------------------------------------------
# Integration: trade_execution.py instruments each gate boundary
# ---------------------------------------------------------------------------

async def _make_rec(db_session, *, symbol="AAPL", suggested_price=Decimal("150.00"),
                    quantity=Decimal("10")):
    from datetime import date
    session = RecommendationSession(session_date=date.today())
    db_session.add(session)
    await db_session.flush()
    rec = TradeRecommendation(
        session_id=session.id, symbol=symbol, action="buy",
        suggested_price=suggested_price, quantity=quantity,
        reasoning="t", confidence="high", status="pending", key_risks="",
    )
    db_session.add(rec)
    await db_session.commit()
    return rec


@pytest.mark.asyncio
async def test_confirm_records_drift_circuit_and_risk_gate_decisions(
    db_session, _override_db, _patch_recorder_session
):
    """When /trades/confirm runs, every gate boundary must persist a decision.

    This is the operator's only way to attribute a non-buy back to a specific
    gate without grepping logs — the explicit Codex ask.
    """
    rec = await _make_rec(db_session)

    fake_broker = AsyncMock()
    fake_broker.submit_buy.return_value = {
        "status": "submitted", "filled_qty": Decimal("10"), "filled_avg_price": Decimal("150.45"),
    }
    risk_pass = MagicMock()
    risk_pass.passed = True
    risk_pass.details = {"sub": "ok"}
    circuit_pass = MagicMock()
    circuit_pass.passed = True

    snapshot = {"AAPL": {"current_price": 150.5, "prev_close": 149.0}}
    with patch("scorched.services.alpaca_data.fetch_snapshots_sync", return_value=snapshot), \
         patch("scorched.services.trade_execution.get_broker", return_value=fake_broker), \
         patch("scorched.services.trade_execution.run_all_buy_gates", return_value=risk_pass), \
         patch("scorched.services.trade_execution.run_circuit_breaker", new=AsyncMock(return_value=[
             {"symbol": "AAPL", "action": "buy", "suggested_price": 150.0, "gate_result": circuit_pass}
         ])):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/api/v1/trades/confirm", json={"recommendation_id": rec.id})
    assert r.status_code == 200

    decisions = (await db_session.execute(
        select(GateDecision).where(GateDecision.recommendation_id == rec.id)
    )).scalars().all()
    by_gate = {d.gate: d for d in decisions}
    assert "drift" in by_gate
    assert by_gate["drift"].passed is True
    assert "risk_gates" in by_gate
    assert by_gate["risk_gates"].passed is True
    assert "circuit_breaker" in by_gate
    assert by_gate["circuit_breaker"].passed is True


@pytest.mark.asyncio
async def test_confirm_records_blocked_decision_when_risk_gate_rejects(
    db_session, _override_db, _patch_recorder_session
):
    """Even on rejection (caller transaction rolls back), the blocked decision persists."""
    rec = await _make_rec(db_session)

    fake_broker = AsyncMock()
    risk_block = MagicMock()
    risk_block.passed = False
    risk_block.reason = "cash_floor: projected $5 < floor $100"
    risk_block.details = {"projected_cash": 5.0, "floor": 100.0}

    snapshot = {"AAPL": {"current_price": 150.5, "prev_close": 149.0}}
    with patch("scorched.services.alpaca_data.fetch_snapshots_sync", return_value=snapshot), \
         patch("scorched.services.trade_execution.get_broker", return_value=fake_broker), \
         patch("scorched.services.trade_execution.run_all_buy_gates", return_value=risk_block):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/api/v1/trades/confirm", json={"recommendation_id": rec.id})
    assert r.status_code == 422
    fake_broker.submit_buy.assert_not_called()

    decisions = (await db_session.execute(
        select(GateDecision).where(GateDecision.recommendation_id == rec.id)
    )).scalars().all()
    risk_decisions = [d for d in decisions if d.gate == "risk_gates"]
    assert len(risk_decisions) == 1
    assert risk_decisions[0].passed is False
    assert "cash_floor" in (risk_decisions[0].reason or "")
