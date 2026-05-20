"""测试 core/subagent.py"""

import pytest

from mxwbot.core.subagent import SubAgentConfig, SubAgentManager
from mxwbot.providers.base import LLMProvider, LLMResponse, TokenUsage


class _FakeProvider(LLMProvider):
    def __init__(self, response="subagent done", delay=0):
        super().__init__(model="test")
        self._response = response
        self._delay = delay

    @property
    def supports_streaming(self) -> bool:
        return True

    async def _chat_impl(self, messages, tools):
        if self._delay:
            import asyncio
            await asyncio.sleep(self._delay)
        return LLMResponse(content=self._response, usage=TokenUsage(input_tokens=1, output_tokens=1))

    async def _chat_stream_impl(self, messages, tools):
        if False:
            yield
        return


class TestSubAgentManager:
    @pytest.mark.asyncio
    async def test_spawn_and_wait(self):
        mgr = SubAgentManager(max_concurrent=5)
        config = SubAgentConfig(
            task="find all .py files",
            provider=_FakeProvider("found 3 .py files"),
        )
        agent_id = await mgr.spawn(config)
        assert mgr.active_count == 1

        result = await mgr.wait(agent_id)
        assert result.status == "completed"
        assert "found 3" in result.content

    @pytest.mark.asyncio
    async def test_cancel(self):
        mgr = SubAgentManager(max_concurrent=5)
        config = SubAgentConfig(
            task="slow task",
            provider=_FakeProvider("will be cancelled", delay=10),
            timeout_seconds=30,
        )
        agent_id = await mgr.spawn(config)
        assert mgr.active_count == 1

        cancelled = await mgr.cancel(agent_id)
        assert cancelled is True

    @pytest.mark.asyncio
    async def test_timeout(self):
        mgr = SubAgentManager(max_concurrent=5)
        config = SubAgentConfig(
            task="slow task",
            provider=_FakeProvider("too slow", delay=5),
            timeout_seconds=0.1,
        )
        agent_id = await mgr.spawn(config)
        result = await mgr.wait(agent_id, timeout=1)
        assert result.status in ("timeout", "cancelled")

    @pytest.mark.asyncio
    async def test_wait_nonexistent(self):
        mgr = SubAgentManager()
        result = await mgr.wait("no-such-id")
        assert result.status == "error"
        assert "不存在" in result.error

    @pytest.mark.asyncio
    async def test_wait_all(self):
        mgr = SubAgentManager(max_concurrent=5)
        for i in range(3):
            config = SubAgentConfig(
                task=f"task {i}",
                provider=_FakeProvider(f"result {i}"),
            )
            await mgr.spawn(config)

        results = await mgr.wait_all()
        assert len(results) == 3
        assert mgr.active_count == 0
