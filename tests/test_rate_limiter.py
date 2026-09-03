"""RateLimiter's usage() snapshot, surfaced in the admin panel (SPEC 6.4)."""

from __future__ import annotations

from bot.services.xbox.client import RateLimiter


async def test_usage_reflects_acquired_calls() -> None:
    limiter = RateLimiter(windows=((5, 15.0), (10, 300.0)))
    assert limiter.usage() == [(0, 5, 15.0), (0, 10, 300.0)]

    await limiter.acquire()
    await limiter.acquire()

    assert limiter.usage() == [(2, 5, 15.0), (2, 10, 300.0)]


async def test_usage_does_not_itself_count_as_a_call() -> None:
    """A read, not an acquire — checking the diagnostic must never eat into
    the budget it is reporting on."""
    limiter = RateLimiter(windows=((1, 15.0),))
    limiter.usage()
    limiter.usage()
    limiter.usage()
    assert limiter.usage() == [(0, 1, 15.0)]
