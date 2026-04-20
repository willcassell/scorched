"""End-to-end tests for the guidance panel API + linter.

These tests exercise the real endpoints against the real on-disk files. They
don't mock out git or the markdown — if the file is reshaped unrecognisably
the tests catch that as a signal to update the parser, not to silently pass.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from scorched.main import app


@pytest.mark.asyncio
async def test_file_endpoint_returns_content_and_sha():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/api/v1/guidance/file")
    assert r.status_code == 200
    body = r.json()
    assert body["content"].startswith("# ") or body["content"].startswith("##") or body["content"].startswith("---")
    assert len(body["sha256"]) == 64
    assert body["bytes"] == len(body["content"].encode("utf-8"))


@pytest.mark.asyncio
async def test_rules_endpoint_returns_every_hard_rule():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/api/v1/guidance/rules")
    assert r.status_code == 200
    rules = r.json()["rules"]
    # File currently ships 9 numbered hard rules. Loosen lower bound so the
    # test survives incremental growth but still flags regressions.
    assert len(rules) >= 7
    nums = [r["number"] for r in rules]
    assert nums == sorted(nums), "rules must come back numbered ascending"
    # Every rule must have non-empty title and body — guards against the
    # regex parser silently dropping fields after a markdown reshape.
    assert all(r["title"] and r["body"] for r in rules)


@pytest.mark.asyncio
async def test_rules_endpoint_renders_overrides_for_toggle_rules():
    """Rules 2, 6, 7 are wired to dashboard toggles — they must carry an
    overrides object (possibly empty dict). Rules with no override mapping
    should return None.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/api/v1/guidance/rules")
    rules = {r["number"]: r for r in r.json()["rules"]}
    for toggle_rule in (2, 6, 7):
        assert toggle_rule in rules, f"rule #{toggle_rule} missing from parse"
        assert rules[toggle_rule]["overrides"] is not None, \
            f"rule #{toggle_rule} should carry overrides dict"
    # Rule #1 has no wiring
    if 1 in rules:
        assert rules[1]["overrides"] is None


@pytest.mark.asyncio
async def test_history_endpoint_returns_commit_entries():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/api/v1/guidance/history?limit=5")
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert len(entries) >= 1, "file should have at least one commit"
    for e in entries:
        assert len(e["sha"]) == 7
        assert e["date"]
        assert e["author"]
        assert e["insertions"] >= 0
        assert e["deletions"] >= 0


@pytest.mark.asyncio
async def test_lint_endpoint_structure():
    """Linter always runs and returns structured findings + counts."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/api/v1/guidance/lint")
    assert r.status_code == 200
    body = r.json()
    assert set(body["counts"].keys()) == {"ok", "info", "warning", "error"}
    for f in body["findings"]:
        assert f["severity"] in ("ok", "info", "warning", "error")
        assert f["message"]


@pytest.mark.asyncio
async def test_firings_endpoint_accepts_date_param():
    """Even with no matching date, the endpoint returns valid shape."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/api/v1/guidance/firings?date=1999-01-01")
    assert r.status_code == 200
    body = r.json()
    assert body["firings"] == []
    assert body["count"] == 0
    assert body["date"] == "1999-01-01"


@pytest.mark.asyncio
async def test_firings_endpoint_rejects_bad_date():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/api/v1/guidance/firings?date=not-a-date")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_guidance_static_page_renders():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/guidance")
    assert r.status_code == 200
    html = r.text
    assert "GUIDANCE" in html
    assert "</html>" in html
    # Sanity: the client-side escape helper exists — guards against a
    # future refactor accidentally removing XSS protection.
    assert "_ESC_MAP" in html


def test_guidance_page_registered_in_dashboard_nav():
    """The GUIDANCE nav entry must land in dashboard.html — otherwise users
    have no way to find the new page."""
    from pathlib import Path
    html = Path(app.state.static_dir if hasattr(app.state, "static_dir") else
                Path(__file__).resolve().parents[1] / "src/scorched/static"
                ).joinpath("dashboard.html").read_text()
    assert 'href="/guidance"' in html
