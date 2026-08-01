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

import json
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

import cron.tradebot_phase2 as tradebot_phase2
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


async def _seed_one_amzn_session(db_session):
    session = RecommendationSession(session_date=date(2026, 7, 31))
    db_session.add(session)
    await db_session.flush()
    db_session.add(TradeRecommendation(
        session_id=session.id, symbol="AMZN", action="buy",
        suggested_price=Decimal("230.00"), quantity=Decimal("10"),
        reasoning="test", confidence="high", status="pending",
    ))
    await db_session.commit()


@pytest.mark.asyncio
async def test_get_recommendations_include_recs_nests_status(db_session, _override_db):
    """include_recs=true (what Phase 2's DB rescue passes) nests full
    recommendation detail with status."""
    await _seed_one_amzn_session(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get(
            "/api/v1/recommendations",
            params={"session_date": "2026-07-31", "limit": 1, "include_recs": "true"},
        )

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    recs = body[0]["recommendations"]
    assert len(recs) == 1
    assert recs[0]["symbol"] == "AMZN"
    assert recs[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_get_recommendations_default_omits_recs_payload(db_session, _override_db):
    """Default (no include_recs): dashboard.html and analysis.html only ever
    read `.id`/`.session_date` off this list endpoint and fetch full detail
    separately via GET /{session_id} — nesting full reasoning/key_risks text
    here by default would be pure unused payload bloat for every list/nav
    call they make (dashboard.html up to limit=5, analysis.html limit=30)."""
    await _seed_one_amzn_session(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/api/v1/recommendations", params={"session_date": "2026-07-31", "limit": 1})

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["recommendations"] == []
    assert body[0]["recommendation_count"] == 1  # count is still accurate even though detail is omitted


# ── main() integration: DB-only rescue when the Phase 1 file is unusable ──
#
# Phase 1 persists recs to the DB via the API BEFORE the cron writes the
# JSON file. A crash between those two steps — or a stale/incomplete file
# left over from a prior run — used to just skip Phase 2 entirely, dropping
# any DB-pending recs on the floor. These tests drive `main()` end-to-end
# (with http_get/http_post/send_telegram/check_expected_hour faked out) to
# confirm the DB-only rescue path fires instead.

def _make_http_get(rules):
    """rules: list of (substring, response-or-callable) checked in order."""
    def _get(path, timeout=60):
        for needle, resp in rules:
            if needle in path:
                return resp() if callable(resp) else resp
        raise AssertionError(f"unexpected http_get path: {path}")
    return _get


def _make_http_post(rules):
    def _post(path, payload, timeout=60):
        for needle, resp in rules:
            if needle in path:
                return resp() if callable(resp) else resp
        raise AssertionError(f"unexpected http_post path: {path}")
    return _post


@pytest.fixture
def _phase2_env(tmp_path, monkeypatch):
    """Point Phase 2's file paths at an empty tmp dir and stub out
    check_expected_hour/send_telegram so main() can run without a real
    cron environment or network access. Returns the list send_telegram
    messages are appended to."""
    monkeypatch.setattr(tradebot_phase2, "GATED_FILE", str(tmp_path / "gated.json"))
    monkeypatch.setattr(tradebot_phase2, "ORIGINAL_FILE", str(tmp_path / "original.json"))
    monkeypatch.setattr(tradebot_phase2, "check_expected_hour", lambda *a, **k: None)

    sent = []
    monkeypatch.setattr(tradebot_phase2, "send_telegram", lambda msg: sent.append(msg))
    return sent


def _db_session_response(recs):
    """Shape of GET /api/v1/recommendations?session_date=...&limit=1."""
    return [{"id": 1, "session_date": "2026-01-01", "recommendation_count": len(recs),
             "created_at": "2026-01-01T00:00:00", "recommendations": recs}]


def _patch_cb_passthrough(monkeypatch):
    """Replace the rescue path's inline circuit breaker with a recording
    pass-through. Returns the list of rec-lists it was called with, so tests
    can assert the breaker was consulted without hitting live market data."""
    calls = []

    def _cb(recs):
        calls.append(list(recs))
        return recs, ""

    monkeypatch.setattr(tradebot_phase2, "_apply_circuit_breaker", _cb)
    return calls


def test_missing_file_with_db_pending_executes_with_warning(_phase2_env, monkeypatch):
    """No Phase 1/1.5 file at all, but the DB has a pending buy for today's
    session (Phase 1's API call persisted it before the cron process died
    before writing the JSON). Phase 2 must rescue and execute it, loudly —
    and must consult the circuit breaker inline (Phase 1.5 never saw these)."""
    sent = _phase2_env
    cb_calls = _patch_cb_passthrough(monkeypatch)
    db_recs = [{"id": 42, "symbol": "AMZN", "action": "buy", "status": "pending",
                "suggested_price": "230.00", "quantity": "10"}]

    get_rules = [
        ("recommendations?session_date=", _db_session_response(db_recs)),
        ("broker/status", {"broker_mode": "paper"}),
        ("current-prices", {"current_prices": {"AMZN": 235.0}}),
        ("opening-prices", {"opening_prices": {"AMZN": 233.0}}),
        ("/api/v1/portfolio", {"total_value": 100000.0, "all_time_return_pct": 1.2,
                                "cash_balance": 5000.0, "positions": []}),
    ]
    post_rules = [
        ("trades/confirm", {"trade_id": 555, "execution_price": "235.70", "realized_gain": None}),
    ]
    monkeypatch.setattr(tradebot_phase2, "http_get", _make_http_get(get_rules))
    monkeypatch.setattr(tradebot_phase2, "http_post", _make_http_post(post_rules))

    tradebot_phase2.main()

    assert any("no usable Phase 1 file" in m and "no Phase 1 data found" in m for m in sent)
    assert any("executing 1 DB-pending rec(s) (circuit breaker applied inline)" in m for m in sent)
    assert cb_calls and [r["symbol"] for r in cb_calls[0]] == ["AMZN"]
    final = sent[-1]
    assert "PHASE 2 FILE/DB MISMATCH" in final
    assert "buy AMZN" in final
    assert "circuit breaker applied inline" in final
    assert "AMZN" in final and "Trades Executed" in final


def test_missing_file_with_no_db_pending_is_clean_skip(_phase2_env, monkeypatch):
    """No file, no DB-pending recs either — this is a genuinely quiet day,
    not a dropped trade. Must fall back to the original skip message, and
    must never attempt to execute anything."""
    sent = _phase2_env

    def _no_post(path, payload, timeout=60):
        raise AssertionError(f"http_post should not be called on a clean skip: {path}")

    monkeypatch.setattr(tradebot_phase2, "http_get", _make_http_get([
        ("recommendations?session_date=", _db_session_response([])),
    ]))
    monkeypatch.setattr(tradebot_phase2, "http_post", _no_post)

    tradebot_phase2.main()

    assert len(sent) == 1
    assert "Phase 2 skipped: no Phase 1 data found." in sent[0]
    assert "⚠️" not in sent[0]  # no rescue/mismatch warning markers on a genuinely quiet day


def test_date_mismatch_with_db_pending_executes_with_warning(_phase2_env, monkeypatch, tmp_path):
    """A stale ORIGINAL_FILE (from a prior session) is on disk, but the DB
    has a pending buy for today. The stale file must not block the rescue."""
    sent = _phase2_env
    _patch_cb_passthrough(monkeypatch)
    stale_file = tmp_path / "original.json"
    stale_file.write_text(json.dumps({
        "date": "2020-01-01", "status": "complete", "recommendations": [], "symbols": [],
    }))
    monkeypatch.setattr(tradebot_phase2, "ORIGINAL_FILE", str(stale_file))

    db_recs = [{"id": 43, "symbol": "GS", "action": "buy", "status": "pending",
                "suggested_price": "1090.00", "quantity": "9"}]
    get_rules = [
        ("recommendations?session_date=", _db_session_response(db_recs)),
        ("broker/status", {"broker_mode": "paper"}),
        ("current-prices", {"current_prices": {"GS": 1095.0}}),
        ("opening-prices", {"opening_prices": {"GS": 1092.0}}),
        ("/api/v1/portfolio", {"total_value": 100000.0, "all_time_return_pct": 1.2,
                                "cash_balance": 5000.0, "positions": []}),
    ]
    post_rules = [
        ("trades/confirm", {"trade_id": 556, "execution_price": "1096.20", "realized_gain": None}),
    ]
    monkeypatch.setattr(tradebot_phase2, "http_get", _make_http_get(get_rules))
    monkeypatch.setattr(tradebot_phase2, "http_post", _make_http_post(post_rules))

    tradebot_phase2.main()

    assert any("no usable Phase 1 file" in m and "stale (dated 2020-01-01)" in m for m in sent)
    assert any("executing 1 DB-pending rec(s) (circuit breaker applied inline)" in m for m in sent)
    final = sent[-1]
    assert "PHASE 2 FILE/DB MISMATCH" in final
    assert "buy GS" in final
    # Stale file must be cleaned up, not left to confuse tomorrow's run.
    assert not stale_file.exists()


def test_status_incomplete_with_db_pending_rescues_with_circuit_breaker(_phase2_env, monkeypatch, tmp_path):
    """Phase 1 wrote a file but marked it errored (status != complete) while
    the DB already holds today's pending rec. The rescue must run AND must
    consult the circuit breaker inline."""
    sent = _phase2_env
    cb_calls = _patch_cb_passthrough(monkeypatch)
    bad_file = tmp_path / "original.json"
    bad_file.write_text(json.dumps({
        "date": tradebot_phase2.now_et()[1], "status": "error",
        "recommendations": [], "symbols": [],
    }))
    monkeypatch.setattr(tradebot_phase2, "ORIGINAL_FILE", str(bad_file))

    db_recs = [{"id": 44, "symbol": "GS", "action": "buy", "status": "pending",
                "suggested_price": "1090.00", "quantity": "9"}]
    get_rules = [
        ("recommendations?session_date=", _db_session_response(db_recs)),
        ("broker/status", {"broker_mode": "paper"}),
        ("current-prices", {"current_prices": {"GS": 1095.0}}),
        ("opening-prices", {"opening_prices": {"GS": 1092.0}}),
        ("/api/v1/portfolio", {"total_value": 100000.0, "all_time_return_pct": 1.2,
                                "cash_balance": 5000.0, "positions": []}),
    ]
    post_rules = [
        ("trades/confirm", {"trade_id": 557, "execution_price": "1096.20", "realized_gain": None}),
    ]
    monkeypatch.setattr(tradebot_phase2, "http_get", _make_http_get(get_rules))
    monkeypatch.setattr(tradebot_phase2, "http_post", _make_http_post(post_rules))

    tradebot_phase2.main()

    assert any("no usable Phase 1 file" in m and "incomplete (status=error)" in m for m in sent)
    assert cb_calls and [r["symbol"] for r in cb_calls[0]] == ["GS"]
    final = sent[-1]
    assert "buy GS" in final and "Trades Executed" in final


def test_rescue_blocked_entirely_by_circuit_breaker_skips_execution(_phase2_env, monkeypatch):
    """DB rescue finds pending buys but the inline circuit breaker blocks
    them all — nothing may be submitted, and the skip must say why."""
    sent = _phase2_env

    def _cb_blocks_all(recs):
        return [], "CIRCUIT BREAKER blocked during rescue: buy AMZN (SPY gap-down)\n"

    monkeypatch.setattr(tradebot_phase2, "_apply_circuit_breaker", _cb_blocks_all)

    db_recs = [{"id": 45, "symbol": "AMZN", "action": "buy", "status": "pending",
                "suggested_price": "230.00", "quantity": "10"}]

    def _no_post(path, payload, timeout=60):
        raise AssertionError(f"nothing may be submitted when CB blocks the whole rescue: {path}")

    monkeypatch.setattr(tradebot_phase2, "http_get", _make_http_get([
        ("recommendations?session_date=", _db_session_response(db_recs)),
    ]))
    monkeypatch.setattr(tradebot_phase2, "http_post", _no_post)

    tradebot_phase2.main()

    assert any("none survived the circuit breaker" in m and "SPY gap-down" in m for m in sent)


def test_stale_gated_file_does_not_mask_valid_original(_phase2_env, monkeypatch, tmp_path):
    """Yesterday's GATED file + a valid same-day ORIGINAL: Phase 2 must use
    the valid original (normal gated-file-absent semantics), NOT fall into
    the DB rescue, and must not delete the good file before consuming it."""
    sent = _phase2_env
    today = tradebot_phase2.now_et()[1]
    stale_gated = tmp_path / "gated.json"
    stale_gated.write_text(json.dumps({
        "date": "2020-01-01", "status": "complete", "recommendations": [], "symbols": [],
    }))
    valid_original = tmp_path / "original.json"
    valid_original.write_text(json.dumps({
        "date": today, "status": "complete",
        "recommendations": [{"id": 46, "symbol": "AAPL", "action": "buy",
                              "status": "pending", "suggested_price": "200.00", "quantity": "5"}],
        "symbols": ["AAPL"],
    }))
    monkeypatch.setattr(tradebot_phase2, "GATED_FILE", str(stale_gated))
    monkeypatch.setattr(tradebot_phase2, "ORIGINAL_FILE", str(valid_original))

    get_rules = [
        ("recommendations?session_date=", _db_session_response([])),
        ("broker/status", {"broker_mode": "paper"}),
        ("current-prices", {"current_prices": {"AAPL": 201.0}}),
        ("opening-prices", {"opening_prices": {"AAPL": 200.5}}),
        ("/api/v1/portfolio", {"total_value": 100000.0, "all_time_return_pct": 1.2,
                                "cash_balance": 5000.0, "positions": []}),
    ]
    post_rules = [
        ("trades/confirm", {"trade_id": 558, "execution_price": "201.60", "realized_gain": None}),
    ]
    monkeypatch.setattr(tradebot_phase2, "http_get", _make_http_get(get_rules))
    monkeypatch.setattr(tradebot_phase2, "http_post", _make_http_post(post_rules))

    tradebot_phase2.main()

    assert not any("no usable Phase 1 file" in m for m in sent), "must not fall into DB rescue"
    final = sent[-1]
    assert "AAPL" in final and "Trades Executed" in final


def test_fail_closed_merge_skip_alerts_via_telegram(_phase2_env, monkeypatch, tmp_path):
    """GATED_FILE exists (circuit breaker ran) but ORIGINAL_FILE (needed to
    diff out circuit-breaker-blocked buys) is missing. The DB-authority
    merge must fail closed (skip, don't guess) AND alert — silently
    executing file-only recs with no explanation is exactly the kind of
    degraded-mode-nobody-notices bug this task is about."""
    sent = _phase2_env
    gated_file = tmp_path / "gated.json"
    gated_file.write_text(json.dumps({
        "date": tradebot_phase2.now_et()[1],
        "status": "complete",
        "recommendations": [{"id": 1, "symbol": "AAPL", "action": "buy",
                              "status": "pending", "suggested_price": "200.00", "quantity": "5"}],
        "symbols": ["AAPL"],
    }))
    monkeypatch.setattr(tradebot_phase2, "GATED_FILE", str(gated_file))
    # ORIGINAL_FILE left pointing at the (nonexistent) tmp_path default from
    # _phase2_env — i.e. genuinely missing.

    get_rules = [
        ("broker/status", {"broker_mode": "paper"}),
        ("current-prices", {"current_prices": {"AAPL": 201.0}}),
        ("opening-prices", {"opening_prices": {"AAPL": 200.5}}),
        ("/api/v1/portfolio", {"total_value": 100000.0, "all_time_return_pct": 1.2,
                                "cash_balance": 5000.0, "positions": []}),
    ]
    post_rules = [
        ("trades/confirm", {"trade_id": 999, "execution_price": "201.60", "realized_gain": None}),
    ]

    def _get_no_db_recs_call(path, timeout=60):
        assert "recommendations?session_date=" not in path, (
            "fail-closed skip must not fetch DB-pending recs at all"
        )
        return _make_http_get(get_rules)(path, timeout)

    monkeypatch.setattr(tradebot_phase2, "http_get", _get_no_db_recs_call)
    monkeypatch.setattr(tradebot_phase2, "http_post", _make_http_post(post_rules))

    tradebot_phase2.main()

    assert any(
        "DB-authority merge SKIPPED" in m and "original (pre-gate) file missing" in m
        and "will not be rescued today" in m
        for m in sent
    )
    # Execution still proceeds with the file's own rec — fail-closed means
    # "don't guess about the merge," not "abort the whole session."
    final = sent[-1]
    assert "AAPL" in final and "Trades Executed" in final
    assert "PHASE 2 FILE/DB MISMATCH" not in final  # nothing was merged in
