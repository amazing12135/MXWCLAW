"""Tests for utils/async_utils.py — timeout and concurrency limiter."""

import asyncio

import pytest

from mxwbot.utils.async_utils import ConcurrencyLimiter, async_timeout


class TestAsyncTimeout:
    @pytest.mark.asyncio
    async def test_completes_before_timeout(self):
        async with async_timeout(5.0):
            result = await asyncio.sleep(0.01)
            assert result is None

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        with pytest.raises(asyncio.TimeoutError):
            async with async_timeout(0.01):
                await asyncio.sleep(10)

    @pytest.mark.asyncio
    async def test_zero_timeout_skips(self):
        async with async_timeout(0):
            await asyncio.sleep(0.01)  # should not raise


class TestConcurrencyLimiter:
    @pytest.mark.asyncio
    async def test_permits_are_limited(self):
        limiter = ConcurrencyLimiter(2)
        active = 0
        max_active = 0

        async def work():
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1

        async with asyncio.TaskGroup() as tg:
            for _ in range(5):
                tg.create_task(limiter.run(work()))

        assert max_active <= 2

    @pytest.mark.asyncio
    async def test_available_property(self):
        limiter = ConcurrencyLimiter(3)
        assert limiter.available == 3

    def test_invalid_max(self):
        with pytest.raises(ValueError):
            ConcurrencyLimiter(0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
