"""main.py must refuse to boot when broker_mode is live and the PIN is too weak."""
import pytest


def test_startup_refuses_live_mode_with_empty_pin(monkeypatch):
    from scorched import main as main_mod

    monkeypatch.setattr(main_mod.settings, "broker_mode", "alpaca_live")
    monkeypatch.setattr(main_mod.settings, "settings_pin", "")

    with pytest.raises(RuntimeError, match="SETTINGS_PIN"):
        main_mod._assert_live_mode_safe()


def test_startup_refuses_live_mode_with_short_pin(monkeypatch):
    from scorched import main as main_mod

    monkeypatch.setattr(main_mod.settings, "broker_mode", "alpaca_live")
    monkeypatch.setattr(main_mod.settings, "settings_pin", "1234")

    with pytest.raises(RuntimeError, match="at least 16"):
        main_mod._assert_live_mode_safe()


def test_startup_allows_paper_mode_with_any_pin(monkeypatch):
    from scorched import main as main_mod

    monkeypatch.setattr(main_mod.settings, "broker_mode", "paper")
    monkeypatch.setattr(main_mod.settings, "settings_pin", "")
    # Should not raise
    main_mod._assert_live_mode_safe()


def test_startup_allows_live_mode_with_strong_pin(monkeypatch):
    from scorched import main as main_mod

    monkeypatch.setattr(main_mod.settings, "broker_mode", "alpaca_live")
    monkeypatch.setattr(main_mod.settings, "settings_pin", "X" * 20)
    main_mod._assert_live_mode_safe()
