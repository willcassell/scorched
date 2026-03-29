"""Tests for the async retry helper."""
import pytest
import anthropic
from unittest.mock import MagicMock, AsyncMock, patch

from scorched.retry import claude_call_with_retry


class TestClaudeCallWithRetry:
    @pytest.mark.asyncio
    async def test_returns_on_first_success(self):
        client = MagicMock()
        response = MagicMock()
        client.copy.return_value = client
        client.messages.create.return_value = response

        result = await claude_call_with_retry(client, "test", model="test-model", max_tokens=100)

        assert result is response
        assert client.messages.create.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_api_error(self):
        client = MagicMock()
        client.copy.return_value = client

        error_response = MagicMock()
        error_response.status_code = 529
        error = anthropic.APIStatusError(
            message="overloaded", response=error_response, body=None
        )

        success_response = MagicMock()
        client.messages.create.side_effect = [error, success_response]

        with patch("scorched.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await claude_call_with_retry(client, "test", model="m", max_tokens=1)

        assert result is success_response
        assert client.messages.create.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_raises_after_all_retries_exhausted(self):
        client = MagicMock()
        client.copy.return_value = client

        error_response = MagicMock()
        error_response.status_code = 500
        error = anthropic.APIStatusError(
            message="server error", response=error_response, body=None
        )
        client.messages.create.side_effect = error

        with patch("scorched.retry.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(anthropic.APIStatusError):
                await claude_call_with_retry(client, "test", model="m", max_tokens=1)

        assert client.messages.create.call_count == 5

    @pytest.mark.asyncio
    async def test_uses_escalating_delays(self):
        client = MagicMock()
        client.copy.return_value = client

        error_response = MagicMock()
        error_response.status_code = 529
        error = anthropic.APIStatusError(
            message="overloaded", response=error_response, body=None
        )

        success_response = MagicMock()
        client.messages.create.side_effect = [error, error, error, success_response]

        with patch("scorched.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await claude_call_with_retry(client, "test", model="m", max_tokens=1)

        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays == [1, 5, 30]
