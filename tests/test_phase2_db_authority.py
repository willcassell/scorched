"""Phase 2 DB-authority tests.

Three risk-review-cleared buys (ABBV 6/23, GS 7/14, AMZN 7/31) sat pending in
the DB but never got a Phase 2 confirm attempt because the file pipeline
(Phase 1/1.5 JSON) silently dropped them. `trade_recommendations` in Postgres
is the source of truth, not the JSON handoff file.

`merge_pending` unions Phase 2's file recs with today's DB-pending recs so a
rec that survives risk review but never makes it into the file still gets a
confirm attempt. `gate_blocked_keys` prevents that merge from resurrecting
circuit-breaker-blocked buys, which stay status='pending' in the DB (Phase
1.5 never updates DB status) and would otherwise be indistinguishable from a
rec the file pipeline dropped.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from cron.tradebot_phase2 import gate_blocked_keys, merge_pending
from scorched.api.deps import require_owner_pin
from scorched.database import get_db
from scorched.main import app
from scorched.models import RecommendationSession, TradeRecommendation


# ── merge_pending (pure function) ───────────────────────────────────────────

def test_merge_pending_adds_db_only_pending_rec():
    """Brief's canonical case: file has [buy AAPL], DB has [pending buy AAPL,
    pending buy GS, rejected buy XOM] -> merged has GS, missing == ['buy GS']."""
    file_recs = [{"id": 1, "symbol": "AAPL", "action": "buy", "status": "pending", "suggested_price": "100"}]
    db_recs = [
        {"id": 1, "symbol": "AAPL", "action": "buy", "status": "pending", "suggested_price": "100"},
        {"id": 2, "symbol": "GS", "action": "buy", "status": "pending", "suggested_price": "500"},
        {"id": 3, "symbol": "XOM", "action": "buy", "status": "rejected", "suggested_price": "110"},
    ]

    merged, missing = merge_pending(file_recs, db_recs)

    merged_symbols = {r["symbol"] for r in merged}
    assert merged_symbols == {"AAPL", "GS"}
    assert missing == ["buy GS"]


def test_merge_pending_file_entry_wins_on_duplicate():
    """Duplicate (symbol, action) key: file entry (carries gate results) wins,
    not the DB copy."""
    file_recs = [{"id": 1, "symbol": "AAPL", "action": "buy", "status": "pending", "gated": True}]
    db_recs = [{"id": 1, "symbol": "AAPL", "action": "buy", "status": "pending", "gated": False}]

    merged, missing = merge_pending(file_recs, db_recs)

    assert len(merged) == 1
    assert merged[0]["gated"] is True
    assert missing == []


def test_merge_pending_ignores_non_pending_db_recs():
    file_recs = []
    db_recs = [
        {"id": 1, "symbol": "GS", "action": "buy", "status": "submitted"},
        {"id": 2, "symbol": "ABBV", "action": "buy", "status": "confirmed"},
        {"id": 3, "symbol": "PFE", "action": "sell", "status": "rejected"},
    ]

    merged, missing = merge_pending(file_recs, db_recs)

    assert merged == []
    assert missing == []


def test_merge_pending_same_symbol_different_action_both_kept():
    """A symbol can appear as both a pending buy and a pending sell — the key
    is (symbol, action), not symbol alone."""
    file_recs = [{"id": 1, "symbol": "AAPL", "action": "sell", "status": "pending"}]
    db_recs = [{"id": 2, "symbol": "AAPL", "action": "buy", "status": "pending"}]

    merged, missing = merge_pending(file_recs, db_recs)

    assert {(r["symbol"], r["action"]) for r in merged} == {("AAPL", "sell"), ("AAPL", "buy")}
    assert missing == ["buy AAPL"]


def test_merge_pending_empty_file_pure_db_rescue():
    """Empty file + DB-pending is the real-world failure mode this task fixes:
    Phase 1 wrote zero recs to the file but the DB has a pending buy."""
    file_recs = []
    db_recs = [{"id": 5, "symbol": "AMZN", "action": "buy", "status": "pending"}]

    merged, missing = merge_pending(file_recs, db_recs)

    assert len(merged) == 1
    assert merged[0]["symbol"] == "AMZN"
    assert missing == ["buy AMZN"]


# ── gate_blocked_keys (pure function) ───────────────────────────────────────

def test_gate_blocked_keys_identifies_circuit_breaker_rejections():
    original = [
        {"symbol": "AAPL", "action": "buy"},
        {"symbol": "GS", "action": "buy"},
        {"symbol": "MSFT", "action": "sell"},
    ]
    gated = [
        {"symbol": "AAPL", "action": "buy"},
        {"symbol": "MSFT", "action": "sell"},
    ]

    blocked = gate_blocked_keys(original, gated)

    assert blocked == {("GS", "buy")}


def test_gate_blocked_keys_empty_when_nothing_blocked():
    recs = [{"symbol": "AAPL", "action": "buy"}]
    assert gate_blocked_keys(recs, recs) == set()


def test_merge_pending_excludes_circuit_breaker_blocked_rec():
    """End-to-end of the two helpers together: a DB-pending buy that was
    circuit-breaker-blocked must not be resurrected by merge_pending once the
    caller filters db_recs through gate_blocked_keys."""
    original_file_recs = [
        {"symbol": "AAPL", "action": "buy"},
        {"symbol": "GS", "action": "buy"},
    ]
    gated_file_recs = [{"symbol": "AAPL", "action": "buy"}]  # GS blocked by circuit breaker

    blocked = gate_blocked_keys(original_file_recs, gated_file_recs)

    db_recs = [
        {"id": 1, "symbol": "AAPL", "action": "buy", "status": "pending"},
        {"id": 2, "symbol": "GS", "action": "buy", "status": "pending"},  # still 'pending' in DB
    ]
    db_recs_filtered = [r for r in db_recs if (r["symbol"], r["action"]) not in blocked]

    merged, missing = merge_pending(gated_file_recs, db_recs_filtered)

    assert {r["symbol"] for r in merged} == {"AAPL"}
    assert missing == []


# ── GET /api/v1/recommendations returns per-rec status (API surface) ───────

@pytest.fixture
def _override_db(db_session):
    async def _get_db_override():
        yield db_session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[require_owner_pin] = lambda: None
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_recommendations_nests_recs_with_status(db_session, _override_db):
    session = RecommendationSession(session_date=date(2026, 7, 31))
    db_session.add(session)
    await db_session.flush()
    db_session.add(TradeRecommendation(
        session_id=session.id, symbol="AMZN", action="buy",
        suggested_price=Decimal("230.00"), quantity=Decimal("10"),
        reasoning="test", confidence="high", status="pending",
    ))
    await db_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/api/v1/recommendations", params={"session_date": "2026-07-31", "limit": 1})

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    recs = body[0]["recommendations"]
    assert len(recs) == 1
    assert recs[0]["symbol"] == "AMZN"
    assert recs[0]["status"] == "pending"
