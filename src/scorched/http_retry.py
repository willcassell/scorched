"""HTTP retry wrapper for transient errors on external API calls.

Provides retry_get() for requests.get calls and retry_call() for SDK
function calls (e.g. fredapi, finnhub). Both use 3 attempts with
1s / 3s / 5s backoff, retrying only on transient errors.
"""
from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

RETRY_DELAYS = [1, 3, 5]  # seconds between attempts


def is_transient_error(exc: Exception) -> bool:
    """Return True if the exception is likely transient and worth retrying.

    Transient: timeouts, connection errors, HTTP 5xx.
    Not transient: HTTP 4xx (bad request, auth, not found, rate limit).
    """
    # requests-specific types
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(exc, (ConnectionError, ConnectionResetError, ConnectionAbortedError)):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True

    # HTTP status errors
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        if resp is not None:
            return resp.status_code >= 500
        return False

    # Catch-all: check the string representation for timeout / 5xx hints
    msg = str(exc).lower()
    if "timeout" in msg:
        return True
    # Check for 5xx status codes in the message
    for code in ("500", "502", "503", "504"):
        if code in msg:
            return True

    return False


def retry_get(url: str, label: str = "", **kwargs) -> requests.Response:
    """Wrapper around requests.get with retry on transient errors.

    Args:
        url: The URL to GET.
        label: Human-readable label for log messages (e.g. "EDGAR CIK map").
        **kwargs: Passed through to requests.get (params, headers, timeout, etc.).

    Returns:
        The successful Response object.

    Raises:
        The last exception if all retries are exhausted, or immediately
        if the error is non-transient.
    """
    last_exc: Exception | None = None
    attempts = len(RETRY_DELAYS) + 1  # first attempt + retries

    for attempt in range(attempts):
        try:
            resp = requests.get(url, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_exc = exc
            if not is_transient_error(exc):
                raise

            retries_left = len(RETRY_DELAYS) - attempt
            if retries_left <= 0:
                break

            delay = RETRY_DELAYS[attempt]
            tag = f" [{label}]" if label else ""
            logger.warning(
                "Transient error%s (attempt %d/%d), retrying in %ds: %s",
                tag, attempt + 1, attempts, delay, exc,
            )
            time.sleep(delay)

    # All retries exhausted
    raise last_exc  # type: ignore[misc]


def retry_call(func, *args, label: str = "", **kwargs):
    """Wrapper for SDK function calls with retry on transient errors.

    Args:
        func: The callable to invoke (e.g. client.recommendation_trends).
        *args: Positional arguments for func.
        label: Human-readable label for log messages.
        **kwargs: Keyword arguments for func.

    Returns:
        The return value of func(*args, **kwargs).

    Raises:
        The last exception if all retries are exhausted, or immediately
        if the error is non-transient.
    """
    last_exc: Exception | None = None
    attempts = len(RETRY_DELAYS) + 1

    for attempt in range(attempts):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not is_transient_error(exc):
                raise

            retries_left = len(RETRY_DELAYS) - attempt
            if retries_left <= 0:
                break

            delay = RETRY_DELAYS[attempt]
            tag = f" [{label}]" if label else ""
            logger.warning(
                "Transient error%s (attempt %d/%d), retrying in %ds: %s",
                tag, attempt + 1, attempts, delay, exc,
            )
            time.sleep(delay)

    raise last_exc  # type: ignore[misc]
