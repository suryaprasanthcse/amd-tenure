"""Client-side request-rate limit for shared endpoints (decision D-rpm).

The AMD Token Factory allows 30 requests per minute per account. LLMClient (R.15) retries 429s, but a
burst of concurrent requests would exhaust its retries and spend the daily cap on rejected calls.
RateLimitedTransport spaces request starts at least 60/rpm seconds apart, and it plugs into
LLMClient's `transport` argument, so R.15 stays verbatim. Retries pass through the limit too.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable

import httpx


class RateLimiter:
    """Hands out request-start slots at least 60/rpm seconds apart. Idle time does not bank a burst."""

    def __init__(self, rpm: int, clock: Callable[[], float] = time.monotonic):
        if rpm <= 0:
            raise ValueError("rpm must be positive")
        self.rpm = rpm
        self.interval = 60.0 / rpm
        self._clock = clock
        self._next = float("-inf")
        self._lock = threading.Lock()  # no await inside, so it is safe across event loops and threads

    def reserve(self) -> float:
        """Claim the next slot; returns how many seconds to wait before starting the request."""
        with self._lock:
            now = self._clock()
            slot = max(now, self._next)
            self._next = slot + self.interval
            return slot - now


_SHARED: dict[str, RateLimiter] = {}
_SHARED_LOCK = threading.Lock()


def shared_limiter(name: str, rpm: int) -> RateLimiter:
    """One limiter per endpoint (keyed by e.g. its base URL) for the whole process."""
    with _SHARED_LOCK:
        lim = _SHARED.get(name)
        if lim is None:
            lim = _SHARED[name] = RateLimiter(rpm)
        elif lim.rpm != rpm:
            raise ValueError(f"limiter {name!r} already exists at {lim.rpm} rpm")
        return lim


class RateLimitedTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """An httpx transport (sync and async) that waits for a limiter slot before each request.

    The inner transport is created on first use and dropped on close, so one instance can serve
    several clients in a row (each LLMClient closes its transport when it is closed).
    """

    def __init__(self, limiter: RateLimiter,
                 async_factory: Callable[[], httpx.AsyncBaseTransport] = httpx.AsyncHTTPTransport,
                 sync_factory: Callable[[], httpx.BaseTransport] = httpx.HTTPTransport,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 sync_sleep: Callable[[float], None] = time.sleep):
        self.limiter = limiter
        self._async_factory, self._sync_factory = async_factory, sync_factory
        self._sleep, self._sync_sleep = sleep, sync_sleep
        self._async: httpx.AsyncBaseTransport | None = None
        self._sync: httpx.BaseTransport | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self._sleep(self.limiter.reserve())
        if self._async is None:
            self._async = self._async_factory()
        return await self._async.handle_async_request(request)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._sync_sleep(self.limiter.reserve())
        if self._sync is None:
            self._sync = self._sync_factory()
        return self._sync.handle_request(request)

    async def aclose(self) -> None:
        inner, self._async = self._async, None
        if inner is not None:
            await inner.aclose()

    def close(self) -> None:
        inner, self._sync = self._sync, None
        if inner is not None:
            inner.close()
