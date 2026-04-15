"""Shared retry helper for Anthropic API calls."""
import asyncio
import logging

import anthropic

logger = logging.getLogger(__name__)

RETRY_DELAYS = [1, 5, 30, 60]  # seconds between retries

# Only retry on transient/server errors — fail immediately on client errors
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}


async def claude_call_with_retry(client, label: str, **kwargs):
    """Call client.messages.create with escalating retry delays on transient errors.

    Accepts either anthropic.AsyncAnthropic or anthropic.Anthropic. Retries on:
      - APIStatusError with status in 429/5xx/529 (server overload)
      - APITimeoutError (httpx-level timeout waiting for response)
      - APIConnectionError (DNS failure, socket reset mid-request)
    Fails fast on client errors (400/401/403/404) — those indicate bugs, not
    transient failures.

    When given AsyncAnthropic, the messages.create call is awaited so the event
    loop stays responsive while Claude is thinking (important for FastAPI).
    """
    is_async = isinstance(client, anthropic.AsyncAnthropic)
    # Disable the SDK's own retries — we handle them with custom delays
    client = client.with_options(max_retries=0) if hasattr(client, "with_options") else client.copy(max_retries=0)

    last_err = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            if is_async:
                return await client.messages.create(**kwargs)
            return client.messages.create(**kwargs)
        except anthropic.APIStatusError as e:
            last_err = e
            if e.status_code not in _RETRYABLE_STATUS_CODES:
                logger.error("%s failed with non-retryable status %s — not retrying", label, e.status_code)
                raise
            err_label = f"status {e.status_code}"
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
            last_err = e
            err_label = type(e).__name__

        if attempt < len(RETRY_DELAYS):
            delay = RETRY_DELAYS[attempt]
            logger.warning(
                "%s failed (attempt %d/%d, %s) — retrying in %ds",
                label, attempt + 1, len(RETRY_DELAYS) + 1, err_label, delay,
            )
            await asyncio.sleep(delay)
        else:
            raise last_err
