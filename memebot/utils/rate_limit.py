"""Async token-bucket rate limiter, one per upstream provider.

DexScreener data endpoints allow ~300 req/min; profiles/boosts ~60/min; public
Solana RPC ~100/10s. Construct one limiter per provider and `await acquire()`
before each call.
"""
from __future__ import annotations

import asyncio
import time


class AsyncRateLimiter:
    def __init__(self, rate_per_min: float, burst: int | None = None, *,
                 monotonic=time.monotonic, sleep=asyncio.sleep) -> None:
        self.rate = rate_per_min / 60.0                 # tokens / second
        self.capacity = float(burst if burst is not None else max(1, int(rate_per_min)))
        self.tokens = self.capacity
        # injectable clock/sleep keep the token math unit-testable on a virtual clock (defaults are real).
        self._monotonic = monotonic
        self._sleep = sleep
        self._updated = monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: int = 1) -> None:
        # Token accounting happens UNDER the lock; the wait does NOT. Holding the lock across the sleep
        # would serialize every concurrent caller through each other's full back-off (one slow caller
        # stalls the rest even when tokens free up). Compute-under-lock / sleep-outside lets waiters
        # back off concurrently; the loop re-checks under the lock each pass, so tokens never go negative
        # and the rate is still respected.
        while True:
            async with self._lock:
                now = self._monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate
            await self._sleep(wait)
