"""Tests for claude_client pure helper functions (no API mocking needed)."""
from unittest.mock import MagicMock

from scorched.services.claude_client import extract_text, extract_thinking, parse_json_response


def _text_block(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _thinking_block(thinking: str) -> MagicMock:
    block = MagicMock()
    block.type = "thinking"
    block.thinking = thinking
    return block


class TestExtractText:
    def test_extract_text_from_content_blocks(self):
        blocks = [_thinking_block("hmm"), _text_block("hello world")]
        assert extract_text(blocks) == "hello world"

    def test_extract_text_skips_thinking(self):
        blocks = [_thinking_block("internal"), _text_block("result")]
        assert extract_text(blocks) == "result"

    def test_extract_text_empty(self):
        assert extract_text([]) == ""
        assert extract_text([_thinking_block("only thinking")]) == ""


class TestExtractThinking:
    def test_extract_thinking(self):
        blocks = [_thinking_block("deep thoughts"), _text_block("answer")]
        assert extract_thinking(blocks) == "deep thoughts"

    def test_extract_thinking_missing(self):
        blocks = [_text_block("no thinking here")]
        assert extract_thinking(blocks) == ""


class TestParseJsonResponse:
    def test_parse_json_response_clean(self):
        raw = '{"analysis": "looks good", "candidates": ["AAPL"]}'
        result = parse_json_response(raw)
        assert result["analysis"] == "looks good"
        assert result["candidates"] == ["AAPL"]

    def test_parse_json_response_with_fences(self):
        raw = 'Here is the result:\n```json\n{"key": "value"}\n```\nDone.'
        result = parse_json_response(raw)
        assert result == {"key": "value"}

    def test_parse_json_response_invalid(self):
        assert parse_json_response("not json at all") == {}
        assert parse_json_response("") == {}
