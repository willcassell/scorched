"""Onboarding endpoints must require the X-Owner-Pin header when a PIN is configured."""
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_onboarding_status_requires_pin(monkeypatch):
    """GET /api/v1/onboarding/status must 403 without X-Owner-Pin when PIN configured."""
    monkeypatch.setenv("SETTINGS_PIN", "test-pin-long-enough-1234")
    from scorched import config as cfg
    cfg.settings = cfg.Settings()  # reload with new env
    from scorched.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/api/v1/onboarding/status")
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_onboarding_validate_key_requires_pin(monkeypatch):
    monkeypatch.setenv("SETTINGS_PIN", "test-pin-long-enough-1234")
    from scorched import config as cfg
    cfg.settings = cfg.Settings()
    from scorched.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post("/api/v1/onboarding/validate-key", json={"service": "polygon", "key": "x"})
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_onboarding_save_requires_pin(monkeypatch):
    monkeypatch.setenv("SETTINGS_PIN", "test-pin-long-enough-1234")
    from scorched import config as cfg
    cfg.settings = cfg.Settings()
    from scorched.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post("/api/v1/onboarding/save", json={})
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"
