"""Per-broker rate limiting: separate token-bucket budgets for orders and reads, plus an in-flight cap."""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimits:
    orders_per_sec: float
    orders_per_min: int
    reads_per_sec: float
    reads_per_min: int | None = None  # None: no per-minute bucket


class TokenBucket:
    """Classic token bucket. `acquire()` sleeps until a token is available; a lock keeps waiters FIFO."""

    def __init__(self, rate_per_sec: float, capacity: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.rate = rate_per_sec
        self.capacity = capacity
        self._tokens = capacity
        self._clock = clock
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    async def acquire(self) -> None:
        async with self._lock:
            self._refill()
            if self._tokens < 1:
                await asyncio.sleep((1 - self._tokens) / self.rate)
                self._refill()
            self._tokens -= 1


class BrokerLimiter:
    def __init__(self, limits: RateLimits, max_inflight: int = 4) -> None:
        self._orders = [TokenBucket(limits.orders_per_sec, limits.orders_per_sec),
                        TokenBucket(limits.orders_per_min / 60, limits.orders_per_min)]
        self._reads = [TokenBucket(limits.reads_per_sec, max(1.0, limits.reads_per_sec))]
        if limits.reads_per_min:
            self._reads.append(TokenBucket(limits.reads_per_min / 60, limits.reads_per_min))
        self._inflight = asyncio.Semaphore(max_inflight)

    @asynccontextmanager
    async def orders(self) -> AsyncIterator[None]:
        """Wrap exactly one order-placement call."""
        for bucket in self._orders:
            await bucket.acquire()
        async with self._inflight:
            yield

    @asynccontextmanager
    async def reads(self) -> AsyncIterator[None]:
        """Wrap exactly one read call (holdings, get_order, list_orders)."""
        for bucket in self._reads:
            await bucket.acquire()
        async with self._inflight:
            yield
