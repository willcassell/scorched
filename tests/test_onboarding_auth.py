"""Audit C3: onboarding endpoints require BOOTSTRAP_TOKEN until first save completes."""
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_onboarding_status_requires_bootstrap_token(monkeypatch, tmp_path):
    """GET /api/v1/onboarding/status must 403 without X-Bootstrap-Token when token configured."""
    from scorched.api import onboarding as ob_mod
    # Patch onboarding module settings directly (module-level import is stable)
    monkeypatch.setattr(ob_mod.settings, "bootstrap_token", "secret-boot-token")
    # Ensure onboarding is still open (no sentinel)
    monkeypatch.setattr(ob_mod, "_onboarding_open", lambda: True)
    from scorched.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/api/v1/onboarding/status")
    # No X-Bootstrap-Token → 403
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_onboarding_status_wrong_bootstrap_token(monkeypatch, tmp_path):
    """GET /api/v1/onboarding/status must 403 with wrong X-Bootstrap-Token."""
    from scorched.api import onboarding as ob_mod
    monkeypatch.setattr(ob_mod.settings, "bootstrap_token", "correct-token")
    monkeypatch.setattr(ob_mod, "_onboarding_open", lambda: True)
    from scorched.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get(
            "/api/v1/onboarding/status",
            headers={"X-Bootstrap-Token": "wrong-token"},
        )
    assert r.status_code == 403, f"Expected 403 (wrong token), got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_onboarding_validate_key_requires_bootstrap_token(monkeypatch, tmp_path):
    """POST /api/v1/onboarding/validate-key must 403 without X-Bootstrap-Token."""
    from scorched.api import onboarding as ob_mod
    monkeypatch.setattr(ob_mod.settings, "bootstrap_token", "secret-boot-token")
    monkeypatch.setattr(ob_mod, "_onboarding_open", lambda: True)
    from scorched.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post(
            "/api/v1/onboarding/validate-key",
            json={"service": "polygon", "key": "x"},
        )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_onboarding_save_requires_bootstrap_token(monkeypatch, tmp_path):
    """POST /api/v1/onboarding/save must 403 without X-Bootstrap-Token."""
    from scorched.api import onboarding as ob_mod
    monkeypatch.setattr(ob_mod.settings, "bootstrap_token", "secret-boot-token")
    monkeypatch.setattr(ob_mod, "_onboarding_open", lambda: True)
    from scorched.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post("/api/v1/onboarding/save", json={})
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_onboarding_returns_410_after_completed(monkeypatch, tmp_path):
    """Onboarding routes return 410 after sentinel file exists (onboarding completed)."""
    from scorched.api import onboarding as ob_mod
    # Simulate completed onboarding by patching _onboarding_open to return False
    monkeypatch.setattr(ob_mod, "_onboarding_open", lambda: False)
    monkeypatch.setattr(ob_mod.settings, "bootstrap_token", "test-bootstrap-token")
    from scorched.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get(
            "/api/v1/onboarding/status",
            headers={"X-Bootstrap-Token": "test-bootstrap-token"},
        )
    assert r.status_code == 410, f"Expected 410 (completed), got {r.status_code}: {r.text}"
