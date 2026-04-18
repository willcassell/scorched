"""Tests for the async retry helper."""
import pytest
import anthropic
from unittest.mock import MagicMock, AsyncMock, patch

from scorched.retry import claude_call_with_retry


def _make_sync_client(side_effect=None, return_value=None):
    """Build a MagicMock sync Anthropic client.

    claude_call_with_retry calls client.with_options() to disable SDK retries,
    so we need to make with_options() return an object whose messages.create
    is wired to the desired side_effect / return_value.
    """
    inner = MagicMock()
    if side_effect is not None:
        inner.messages.create.side_effect = side_effect
    elif return_value is not None:
        inner.messages.create.return_value = return_value

    # MagicMock is not an instance of anthropic.AsyncAnthropic, so is_async=False
    # and the sync path (client.messages.create) is used. with_options() must
    # return inner so side_effect is applied to the object actually called.
    client = MagicMock()
    client.with_options.return_value = inner
    return client, inner


class TestClaudeCallWithRetry:
    @pytest.mark.asyncio
    async def test_returns_on_first_success(self):
        response = MagicMock()
        client, inner = _make_sync_client(return_value=response)

        result = await claude_call_with_retry(client, "test", model="test-model", max_tokens=100)

        assert result is response
        assert inner.messages.create.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_api_error(self):
        error_response = MagicMock()
        error_response.status_code = 529
        error = anthropic.APIStatusError(
            message="overloaded", response=error_response, body=None
        )

        success_response = MagicMock()
        client, inner = _make_sync_client(side_effect=[error, success_response])

        with patch("scorched.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await claude_call_with_retry(client, "test", model="m", max_tokens=1)

        assert result is success_response
        assert inner.messages.create.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_raises_after_all_retries_exhausted(self):
        error_response = MagicMock()
        error_response.status_code = 500
        error = anthropic.APIStatusError(
            message="server error", response=error_response, body=None
        )
        client, inner = _make_sync_client(side_effect=error)

        with patch("scorched.retry.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(anthropic.APIStatusError):
                await claude_call_with_retry(client, "test", model="m", max_tokens=1)

        assert inner.messages.create.call_count == 5

    @pytest.mark.asyncio
    async def test_uses_escalating_delays(self):
        error_response = MagicMock()
        error_response.status_code = 529
        error = anthropic.APIStatusError(
            message="overloaded", response=error_response, body=None
        )

        success_response = MagicMock()
        client, inner = _make_sync_client(side_effect=[error, error, error, success_response])

        with patch("scorched.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await claude_call_with_retry(client, "test", model="m", max_tokens=1)

        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays == [1, 5, 30]
