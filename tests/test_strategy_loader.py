"""Tests for strategy.json / analyst_guidance path resolution.

The resolver must anchor on the repo root, not Path.cwd(), so cron scripts
that run from /home/ubuntu still find the files in /home/ubuntu/tradebot.
"""
import pytest


def test_resolve_path_anchors_on_repo_root_not_cwd(tmp_path, monkeypatch):
    """Relative strategy_file should resolve against the package root, not Path.cwd()."""
    from scorched.services import strategy as strat

    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    resolved = strat._resolve_path()
    assert resolved != elsewhere / "strategy.json", (
        f"Expected repo-anchored path, got cwd-anchored: {resolved}"
    )
    assert resolved.exists(), f"Path {resolved} does not exist"


def test_load_strategy_json_from_unrelated_cwd(tmp_path, monkeypatch):
    """load_strategy_json() must return the real config even when cwd is wrong."""
    from scorched.services import strategy as strat

    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    data = strat.load_strategy_json()
    assert "concentration" in data
    # Real strategy.json has circuit_breaker after this change ships
    assert "circuit_breaker" in data
    assert data["circuit_breaker"]["enabled"] is True


def test_load_analyst_guidance_from_unrelated_cwd(tmp_path, monkeypatch):
    """load_analyst_guidance() must find the file from any cwd."""
    from scorched.services import strategy as strat

    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    content = strat.load_analyst_guidance()
    assert len(content) > 100, f"Expected non-empty guidance, got {len(content)} chars"
