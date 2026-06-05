"""Async token-bucket rate limiter, one per upstream provider.

DexScreener data endpoints allow ~300 req/min; profiles/boosts ~60/min; public
Solana RPC ~100/10s. Construct one limiter per provider and `await acquire()`
before each call.
"""
from __future__ import annotations

import asyncio
import time


class AsyncRateLimiter:
    def __init__(self, rate_per_min: float, burst: int | None = None) -> None:
        self.rate = rate_per_min / 60.0                 # tokens / second
        self.capacity = float(burst if burst is not None else max(1, int(rate_per_min)))
        self.tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: int = 1) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                await asyncio.sleep((n - self.tokens) / self.rate)
