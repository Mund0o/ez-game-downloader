"""Politeness limits for websites and bandwidth limits for transfers."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict


class HostRateLimiter:
    """Minimum spacing between requests to the same host, plus a per-host cap."""

    def __init__(self, min_interval: float = 1.0, per_host: int = 2,
                 overrides: dict[str, float] | None = None):
        self.min_interval = min_interval
        self.overrides = overrides or {}
        self._next: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._sems: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(per_host))

    def _interval(self, host: str) -> float:
        for suffix, iv in self.overrides.items():
            if host == suffix or host.endswith("." + suffix):
                return iv
        return self.min_interval

    async def acquire(self, host: str) -> None:
        async with self._locks[host]:
            now = time.monotonic()
            wait = self._next[host] - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next[host] = max(now, self._next[host]) + self._interval(host)

    def slot(self, host: str) -> asyncio.Semaphore:
        return self._sems[host]


class TokenBucket:
    """Shared byte-rate limiter. ``rate`` in bytes/second, 0 = unlimited."""

    def __init__(self, rate: int = 0, burst_seconds: float = 0.5):
        self._rate = 0
        self._tokens = 0.0
        self._last = time.monotonic()
        self._burst_seconds = burst_seconds
        self._lock = asyncio.Lock()
        self.set_rate(rate)

    @property
    def rate(self) -> int:
        return self._rate

    def set_rate(self, rate: int) -> None:
        self._rate = max(0, int(rate))
        self._tokens = min(self._tokens, self._capacity)

    @property
    def _capacity(self) -> float:
        return max(self._rate * self._burst_seconds, 16 * 1024)

    async def consume(self, n: int) -> None:
        if self._rate <= 0 or n <= 0:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= n or self._tokens >= self._capacity:
                    self._tokens -= n            # may go negative for big chunks → pays later
                    return
                await asyncio.sleep(min(1.0, (min(n, self._capacity) - self._tokens) / self._rate))
                if self._rate <= 0:
                    return
