"""Audit C2: missing PIN must fail startup in any mode that exposes mutations."""
import pytest

from scorched.main import _assert_auth_safe


def test_paper_mode_requires_pin(monkeypatch):
    monkeypatch.setattr("scorched.main.settings.broker_mode", "paper")
    monkeypatch.setattr("scorched.main.settings.settings_pin", "")
    with pytest.raises(RuntimeError, match="SETTINGS_PIN"):
        _assert_auth_safe()


def test_alpaca_paper_mode_requires_pin(monkeypatch):
    monkeypatch.setattr("scorched.main.settings.broker_mode", "alpaca_paper")
    monkeypatch.setattr("scorched.main.settings.settings_pin", "")
    with pytest.raises(RuntimeError, match="SETTINGS_PIN"):
        _assert_auth_safe()


def test_alpaca_live_requires_long_pin(monkeypatch):
    monkeypatch.setattr("scorched.main.settings.broker_mode", "alpaca_live")
    monkeypatch.setattr("scorched.main.settings.settings_pin", "short")
    with pytest.raises(RuntimeError, match="too short"):
        _assert_auth_safe()


def test_paper_mode_passes_with_short_pin(monkeypatch):
    monkeypatch.setattr("scorched.main.settings.broker_mode", "paper")
    monkeypatch.setattr("scorched.main.settings.settings_pin", "1234")
    _assert_auth_safe()  # any non-empty PIN ok in paper mode


def test_alpaca_live_requires_kill_switch(monkeypatch):
    """Decision D5: BROKER_MODE=alpaca_live without LIVE_TRADING_ENABLED=true must refuse."""
    monkeypatch.setattr("scorched.main.settings.broker_mode", "alpaca_live")
    monkeypatch.setattr("scorched.main.settings.settings_pin", "x" * 16)
    monkeypatch.setattr("scorched.main.settings.live_trading_enabled", False)
    with pytest.raises(RuntimeError, match="LIVE_TRADING_ENABLED"):
        _assert_auth_safe()


def test_alpaca_live_passes_with_both_set(monkeypatch):
    monkeypatch.setattr("scorched.main.settings.broker_mode", "alpaca_live")
    monkeypatch.setattr("scorched.main.settings.settings_pin", "x" * 16)
    monkeypatch.setattr("scorched.main.settings.live_trading_enabled", True)
    _assert_auth_safe()
