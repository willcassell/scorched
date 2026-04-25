"""main.py must refuse to boot when PIN is unset in any mode, or too short for live mode."""
import pytest


def test_startup_refuses_any_mode_with_empty_pin(monkeypatch):
    from scorched import main as main_mod

    monkeypatch.setattr(main_mod.settings, "broker_mode", "paper")
    monkeypatch.setattr(main_mod.settings, "settings_pin", "")

    with pytest.raises(RuntimeError, match="SETTINGS_PIN"):
        main_mod._assert_auth_safe()


def test_startup_refuses_live_mode_with_empty_pin(monkeypatch):
    from scorched import main as main_mod

    monkeypatch.setattr(main_mod.settings, "broker_mode", "alpaca_live")
    monkeypatch.setattr(main_mod.settings, "settings_pin", "")

    with pytest.raises(RuntimeError, match="SETTINGS_PIN"):
        main_mod._assert_auth_safe()


def test_startup_refuses_live_mode_with_short_pin(monkeypatch):
    from scorched import main as main_mod

    monkeypatch.setattr(main_mod.settings, "broker_mode", "alpaca_live")
    monkeypatch.setattr(main_mod.settings, "settings_pin", "1234")

    with pytest.raises(RuntimeError, match="too short"):
        main_mod._assert_auth_safe()


def test_startup_allows_paper_mode_with_any_nonempty_pin(monkeypatch):
    from scorched import main as main_mod

    monkeypatch.setattr(main_mod.settings, "broker_mode", "paper")
    monkeypatch.setattr(main_mod.settings, "settings_pin", "1234")
    # Any non-empty PIN is acceptable in paper mode
    main_mod._assert_auth_safe()


def test_startup_allows_live_mode_with_strong_pin(monkeypatch):
    from scorched import main as main_mod

    monkeypatch.setattr(main_mod.settings, "broker_mode", "alpaca_live")
    monkeypatch.setattr(main_mod.settings, "settings_pin", "X" * 20)
    main_mod._assert_auth_safe()
