"""Shared retry helper for Anthropic API calls."""
import asyncio
import logging

import anthropic

logger = logging.getLogger(__name__)

RETRY_DELAYS = [1, 5, 30, 60]  # seconds between retries


async def claude_call_with_retry(client: anthropic.Anthropic, label: str, **kwargs):
    """Call client.messages.create with escalating retry delays on API errors.

    The SDK call itself is synchronous; only the sleep between retries is async
    so that we don't block the event loop during back-off waits.
    """
    # Disable the SDK's own retries — we handle them with custom delays
    client = client.copy(max_retries=0)
    last_err = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return client.messages.create(**kwargs)
        except anthropic.APIStatusError as e:
            last_err = e
            if attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                logger.warning(
                    "%s failed (attempt %d/%d, status %s) — retrying in %ds",
                    label, attempt + 1, len(RETRY_DELAYS) + 1, e.status_code, delay,
                )
                await asyncio.sleep(delay)
            else:
                raise last_err
