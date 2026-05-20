"""Tests for memory/core.py — MemoryManager integration."""

import pytest

from mxwbot.memory.core import MemoryManager
from mxwbot.providers.base import LLMProvider, LLMResponse


class _FakeProvider(LLMProvider):
    @property
    def supports_streaming(self) -> bool:
        return True

    async def _chat_impl(self, messages, tools):
        content = messages[-1]["content"]
        if "Extract key facts" in content:
            return LLMResponse(
                content='[{"content":"User prefers dark mode","category":"preference","importance":0.8}]'
            )
        return LLMResponse(content="Summary.")

    async def _chat_stream_impl(self, messages, tools):
        if False:
            yield
        return


class TestMemoryManager:
    @pytest.fixture
    async def mm(self, tmp_path):
        mgr = MemoryManager(tmp_path / ".mxwbot", _FakeProvider(model="test"))
        await mgr.init_db()
        return mgr

    @pytest.mark.asyncio
    async def test_init_db_creates_schema(self, mm):
        assert mm._initialised is True

    @pytest.mark.asyncio
    async def test_long_term_in_context(self, mm):
        await mm.add_memory("Project uses Python", category="project_info")
        ctx = await mm.get_context(query="Python")
        assert "Python" in ctx

    @pytest.mark.asyncio
    async def test_consolidate(self, mm):
        msgs = [{"role": "user", "content": f"msg-{i}"} for i in range(30)]
        new_count, summary = await mm.consolidate(msgs, consolidated_count=0, usage_ratio=0.9, msg_count=30)
        assert new_count >= 15

    @pytest.mark.asyncio
    async def test_force_consolidate(self, mm):
        msgs = [{"role": "user", "content": f"msg-{i}"} for i in range(5)]
        new_count, summary = await mm.force_consolidate(msgs, consolidated_count=0)
        assert new_count == 2

    @pytest.mark.asyncio
    async def test_add_memory(self, mm):
        eid = await mm.add_memory("test fact", "fact", 0.7)
        assert eid
        ctx = await mm.get_context(query="test fact")
        assert "test fact" in ctx
