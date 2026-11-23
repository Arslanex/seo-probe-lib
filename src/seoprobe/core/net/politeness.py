"""Per-host politeness: bounded concurrency, minimum spacing and a circuit breaker."""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class HostLimiter:
    def __init__(self, concurrency: int = 2, min_delay_s: float = 0.4, breaker_after: int = 5) -> None:
        self._concurrency = concurrency
        self._min_delay = min_delay_s
        self._breaker_after = breaker_after
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._next_ok: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = {}
        self._fails: dict[str, int] = defaultdict(int)

    def _sem(self, host: str) -> asyncio.Semaphore:
        if host not in self._sems:
            self._sems[host] = asyncio.Semaphore(self._concurrency)
            self._locks[host] = asyncio.Lock()
        return self._sems[host]

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        sem = self._sem(host)
        async with sem:
            async with self._locks[host]:  # serialise the *start* of requests to enforce spacing
                now = time.monotonic()
                wait = self._next_ok[host] - now
                if wait > 0:
                    await asyncio.sleep(wait)
                self._next_ok[host] = time.monotonic() + self._min_delay * (0.8 + 0.4 * random.random())
            yield

    def record(self, host: str, ok: bool) -> None:
        self._fails[host] = 0 if ok else self._fails[host] + 1

    def is_open(self, host: str) -> bool:
        """True when the circuit breaker tripped (too many consecutive failures)."""
        return self._fails[host] >= self._breaker_after
