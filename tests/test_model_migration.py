"""Tests for the Opus 5 model migration (claude_client.py + cost.py)."""
from scorched.services import claude_client
from scorched import cost


def test_model_is_opus5():
    assert claude_client.MODEL == "claude-opus-5"
    assert not hasattr(claude_client, "HAIKU_MODEL")


def test_no_budget_tokens_anywhere():
    import inspect
    src = inspect.getsource(claude_client)
    assert "budget_tokens" not in src


def test_opus5_pricing():
    assert cost._PRICING["claude-opus-5"] == (5.0, 25.0, 5.0)


def test_refusal_guard_raises():
    import pytest

    class R:
        stop_reason = "refusal"
        stop_details = None

    with pytest.raises(claude_client.ClaudeRefusalError):
        claude_client._refusal_guard(R())
