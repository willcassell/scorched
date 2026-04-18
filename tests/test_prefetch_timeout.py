"""Phase 0 gather must be bounded so one hung source can't hang the whole phase."""
import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_gather_with_timeout_raises_on_hang():
    from scorched.api.prefetch import _gather_with_timeout

    async def hang():
        await asyncio.sleep(3600)

    start = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await _gather_with_timeout([hang()], timeout_s=1)
    elapsed = time.monotonic() - start
    assert elapsed < 3, f"Should have timed out in ~1s, took {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_gather_with_timeout_returns_exceptions_not_raises():
    from scorched.api.prefetch import _gather_with_timeout

    async def ok():
        return "good"

    async def boom():
        raise RuntimeError("one source failed")

    results = await _gather_with_timeout([ok(), boom()], timeout_s=5)
    assert results[0] == "good"
    assert isinstance(results[1], RuntimeError)


def test_phase0_timeout_constant_is_reasonable():
    from scorched.api.prefetch import PHASE0_GATHER_TIMEOUT_S
    # Must be generous enough for a slow day but bounded under Phase 1's start-time delta.
    assert 300 <= PHASE0_GATHER_TIMEOUT_S <= 600
