"""Tests for claude_client pure helper functions (no API mocking needed)."""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from scorched.services.claude_client import (
    extract_text, extract_thinking, parse_json_response,
    call_position_review, call_eod_review, call_intraday_exit,
)


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


class TestCallWrappersUseRetry:
    """All call_* wrappers should use claude_call_with_retry."""

    @pytest.mark.asyncio
    @patch("scorched.services.claude_client.claude_call_with_retry", new_callable=AsyncMock)
    @patch("scorched.services.claude_client._client")
    async def test_call_position_review_uses_retry(self, mock_client, mock_retry):
        mock_response = MagicMock()
        mock_response.content = [_text_block("result")]
        mock_retry.return_value = mock_response
        response, text = await call_position_review("test prompt")
        mock_retry.assert_called_once()
        assert text == "result"

    @pytest.mark.asyncio
    @patch("scorched.services.claude_client.claude_call_with_retry", new_callable=AsyncMock)
    @patch("scorched.services.claude_client._client")
    async def test_call_eod_review_uses_retry(self, mock_client, mock_retry):
        mock_response = MagicMock()
        mock_response.content = [_text_block("  updated playbook  ")]
        mock_retry.return_value = mock_response
        response, text = await call_eod_review("test prompt")
        mock_retry.assert_called_once()
        assert text == "updated playbook"

    @pytest.mark.asyncio
    @patch("scorched.services.claude_client.claude_call_with_retry", new_callable=AsyncMock)
    @patch("scorched.services.claude_client._client")
    async def test_call_intraday_exit_uses_retry(self, mock_client, mock_retry):
        mock_response = MagicMock()
        mock_response.content = [_text_block('{"action": "hold"}')]
        mock_retry.return_value = mock_response
        response, text = await call_intraday_exit("test prompt")
        mock_retry.assert_called_once()
        assert text == '{"action": "hold"}'


def test_risk_decision_entry_captures_action():
    """RiskDecisionEntry must preserve the action field from Claude's output."""
    from scorched.services.claude_client import RiskDecisionEntry, RiskReviewOutput

    raw = {
        "decisions": [
            {"symbol": "aapl", "action": "BUY", "verdict": "reject", "reason": "too extended"},
            {"symbol": "MSFT", "action": "sell", "verdict": "APPROVE", "reason": "fine"},
        ]
    }
    validated = RiskReviewOutput.model_validate(raw)
    dumped = [d.model_dump() for d in validated.decisions]

    # action is lowercased, symbol is uppercased (existing validator), verdict lowercased
    assert dumped[0]["symbol"] == "AAPL"
    assert dumped[0]["action"] == "buy"
    assert dumped[0]["verdict"] == "reject"
    assert dumped[1]["action"] == "sell"
    assert dumped[1]["verdict"] == "approve"
