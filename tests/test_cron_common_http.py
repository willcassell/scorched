"""http_get must inject X-Owner-Pin when SETTINGS_PIN is set (mirrors http_post behaviour)."""
import json
import urllib.error
import urllib.request


class _MockResponse:
    """Minimal stand-in for urllib response."""
    def __init__(self, body=b'{"ok": true}', status=200):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_http_get_injects_pin_when_settings_pin_set(monkeypatch):
    from cron import common

    monkeypatch.setenv("SETTINGS_PIN", "test-secret-pin")
    monkeypatch.setenv("TRADEBOT_URL", "http://localhost:8000")

    captured = {}

    def fake_urlopen(req, timeout=60):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        return _MockResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = common.http_get("/api/v1/portfolio")

    assert result == {"ok": True}
    # Header keys from urllib are title-cased
    assert captured["headers"].get("X-owner-pin") == "test-secret-pin", (
        f"X-Owner-Pin header not sent; captured headers: {captured['headers']}"
    )


def test_http_get_no_pin_header_when_settings_pin_absent(monkeypatch):
    from cron import common

    monkeypatch.delenv("SETTINGS_PIN", raising=False)
    monkeypatch.setenv("TRADEBOT_URL", "http://localhost:8000")

    captured = {}

    def fake_urlopen(req, timeout=60):
        captured["headers"] = dict(req.headers)
        return _MockResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    common.http_get("/api/v1/portfolio")

    assert "X-owner-pin" not in captured["headers"], (
        "X-Owner-Pin should not be sent when SETTINGS_PIN is unset"
    )


def test_http_get_403_raises_descriptive_error(monkeypatch):
    from cron import common
    import pytest

    monkeypatch.setenv("SETTINGS_PIN", "wrongpin")
    monkeypatch.setenv("TRADEBOT_URL", "http://localhost:8000")

    def fake_urlopen(req, timeout=60):
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {}, None
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        common.http_get("/api/v1/portfolio")

    assert "PIN mismatch" in str(exc_info.value.reason)
