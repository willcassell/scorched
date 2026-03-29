"""Tests for prompt loading utility."""
import pytest
from scorched.prompts import load_prompt


def test_load_existing_prompt():
    text = load_prompt("analysis")
    assert len(text) > 100
    assert "analyst" in text.lower() or "analysis" in text.lower()


def test_load_missing_prompt():
    with pytest.raises(FileNotFoundError):
        load_prompt("nonexistent_prompt_xyz")
