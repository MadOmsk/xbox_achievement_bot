"""A generic sliding-window rate limiter (2026-09-05 refactor) — lived in
services/xbox/client.py alone until Steam got its own client-side limiter
too. Platform-agnostic on purpose: neither client needs to know about the
other, this is just shared low-level plumbing underneath both.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque

log = logging.getLogger(__name__)


class RateLimiter:
    """Sliding windows, shared by every user of the bot."""

    def __init__(self, windows: tuple[tuple[int, float], ...]) -> None:
        self._windows = [(limit, span, deque[float]()) for limit, span in windows]
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = asyncio.get_running_loop().time()
                wait = 0.0
                for limit, span, calls in self._windows:
                    while calls and now - calls[0] > span:
                        calls.popleft()
                    if len(calls) >= limit:
                        wait = max(wait, span - (now - calls[0]))
                if wait <= 0:
                    for _, _, calls in self._windows:
                        calls.append(now)
                    return
            log.debug("rate limiter sleeping for %.1fs", wait)
            await asyncio.sleep(wait)

    def usage(self) -> list[tuple[int, int, float]]:
        """A snapshot of (used, limit, window_seconds) per window, for the
        admin panel's API diagnostic (SPEC 6.4) — a read, not an acquire, so
        checking it never itself counts against the budget."""
        now = asyncio.get_running_loop().time()
        result = []
        for limit, span, calls in self._windows:
            while calls and now - calls[0] > span:
                calls.popleft()
            result.append((len(calls), limit, span))
        return result
