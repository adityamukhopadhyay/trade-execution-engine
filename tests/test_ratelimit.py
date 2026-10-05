"""TokenBucket / BrokerLimiter behaviour with a fake clock and a patched asyncio.sleep (no real waiting)."""
from __future__ import annotations

import asyncio

import pytest

from app.brokers import ratelimit
from app.brokers.ratelimit import BrokerLimiter, RateLimits, TokenBucket

pytestmark = pytest.mark.asyncio


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """A clock that only advances when the limiter sleeps; `clock.slept` records every sleep."""
    fake = FakeClock()
    fake.slept = []

    async def fake_sleep(seconds: float) -> None:
        fake.slept.append(seconds)
        fake.now += seconds

    monkeypatch.setattr(ratelimit.asyncio, "sleep", fake_sleep)
    return fake


async def test_bucket_sleeps_once_tokens_run_out(clock):
    bucket = TokenBucket(rate_per_sec=5, capacity=5, clock=clock)
    for _ in range(10):
        await bucket.acquire()
    assert len(clock.slept) == 5                    # the first five were free
    assert sum(clock.slept) == pytest.approx(1.0)   # then 5 at 5/s: 1 s


async def test_bucket_refills_with_time(clock):
    bucket = TokenBucket(rate_per_sec=2, capacity=2, clock=clock)
    await bucket.acquire()
    await bucket.acquire()
    clock.now += 1.0                                # two tokens refilled
    await bucket.acquire()
    await bucket.acquire()
    assert clock.slept == []


async def test_per_minute_bucket_caps_a_burst(clock):
    limiter = BrokerLimiter(RateLimits(orders_per_sec=100, orders_per_min=3, reads_per_sec=100), max_inflight=8)
    limiter._orders = [TokenBucket(100, 100, clock=clock), TokenBucket(3 / 60, 3, clock=clock)]
    for _ in range(3):
        async with limiter.orders():
            pass
    assert clock.slept == []
    async with limiter.orders():                    # 4th order waits for a token
        pass
    assert clock.slept == [pytest.approx(20.0)]


async def test_orders_and_reads_budgets_are_independent(clock):
    limiter = BrokerLimiter(RateLimits(orders_per_sec=1, orders_per_min=1000, reads_per_sec=10, reads_per_min=None))
    limiter._orders = [TokenBucket(1, 1, clock=clock), TokenBucket(1000 / 60, 1000, clock=clock)]
    limiter._reads = [TokenBucket(10, 10, clock=clock)]
    async with limiter.orders():
        pass
    for _ in range(10):
        async with limiter.reads():
            pass
    assert clock.slept == []
    async with limiter.orders():
        pass
    assert clock.slept == [pytest.approx(1.0)]


async def test_semaphore_bounds_in_flight_calls():
    limiter = BrokerLimiter(RateLimits(orders_per_sec=1000, orders_per_min=100000, reads_per_sec=1000), max_inflight=2)
    state = {"inflight": 0, "peak": 0}

    async def one_read() -> None:
        async with limiter.reads():
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
            await asyncio.sleep(0.005)
            state["inflight"] -= 1

    await asyncio.gather(*(one_read() for _ in range(8)))
    assert state["peak"] == 2 and state["inflight"] == 0
