"""Sensitive read endpoints require owner PIN by default."""

import pytest
from httpx import ASGITransport, AsyncClient

from scorched.main import app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/portfolio",
        "/api/v1/recommendations",
        "/api/v1/broker/status",
        "/api/v1/playbook",
        "/api/v1/strategy",
        "/api/v1/system/health",
        "/api/v1/system/errors",
        "/api/v1/system/trend",
    ],
)
async def test_sensitive_read_endpoints_require_pin(monkeypatch, path):
    monkeypatch.setattr("scorched.config.settings.settings_pin", "1234")
    monkeypatch.setattr("scorched.api.deps.settings.settings_pin", "1234")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get(path)

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_health_returns_503_and_scrubs_details_when_db_fails(monkeypatch):
    class FailingSession:
        async def __aenter__(self):
            raise RuntimeError("postgresql://secret-user:secret-pass@db/internal")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("scorched.main.AsyncSessionLocal", lambda: FailingSession())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body == {"status": "unhealthy", "db": "error"}
    assert "secret" not in str(body).lower()


@pytest.mark.asyncio
async def test_liveness_stays_green_independent_of_db():
    """Liveness probe must not depend on the database — that is the whole point of splitting it."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_market_date_stays_public(monkeypatch):
    monkeypatch.setattr("scorched.config.settings.settings_pin", "1234")
    monkeypatch.setattr("scorched.api.deps.settings.settings_pin", "1234")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/v1/system/market-date")

    assert response.status_code == 200
    assert "date" in response.json()
