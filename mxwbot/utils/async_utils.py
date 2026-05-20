"""Async utilities: timeout context manager and concurrency limiter."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


@asynccontextmanager
async def async_timeout(seconds: float) -> AsyncIterator[None]:
    """Context manager that raises asyncio.TimeoutError after *seconds*.

    Usage::

        async with async_timeout(30):
            result = await long_running_task()
    """
    if seconds <= 0:
        yield
        return

    task = asyncio.current_task()
    if task is None:
        yield
        return

    loop = asyncio.get_running_loop()

    def _on_timeout() -> None:
        if task and not task.done():
            task.cancel()

    handle = loop.call_later(seconds, _on_timeout)
    try:
        yield
    except asyncio.CancelledError:
        raise asyncio.TimeoutError(f"Operation timed out after {seconds}s")
    finally:
        handle.cancel()


class ConcurrencyLimiter:
    """Limit the number of concurrent async operations.

    Usage::

        limiter = ConcurrencyLimiter(5)
        async with limiter:
            await do_work()
    """

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._semaphore = asyncio.Semaphore(max_concurrent)

    @property
    def available(self) -> int:
        """Number of permits currently available."""
        return self._semaphore._value  # type: ignore[attr-defined]

    async def __aenter__(self) -> None:
        await self._semaphore.acquire()

    async def __aexit__(self, *args: object) -> None:
        self._semaphore.release()

    async def run(self, coro):  # type: ignore[no-untyped-def]
        """Run a coroutine inside the limiter's semaphore."""
        async with self:
            return await coro
