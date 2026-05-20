"""Tests for memory/update.py."""

import pytest

from mxwbot.memory.long_term_memory import LongTermMemory
from mxwbot.memory.summarizer import MemorySummarizer
from mxwbot.memory.update import MemoryUpdater
from mxwbot.providers.base import LLMProvider, LLMResponse


class _FakeProvider(LLMProvider):
    def __init__(self, response_content=""):
        super().__init__(model="test")
        self._response = response_content or (
            '[{"content":"User prefers dark mode","category":"preference","importance":0.8},'
            '{"content":"Uses pytest","category":"project_info","importance":0.6}]'
        )

    @property
    def supports_streaming(self) -> bool:
        return True

    async def _chat_impl(self, messages, tools):
        return LLMResponse(content=self._response)

    async def _chat_stream_impl(self, messages, tools):
        if False:
            yield
        return


class TestMemoryUpdater:
    @pytest.fixture
    async def updater(self, tmp_path):
        ltm = LongTermMemory(tmp_path / "test.db")
        await ltm.init_db()
        return MemoryUpdater(ltm, MemorySummarizer(_FakeProvider()))

    @pytest.mark.asyncio
    async def test_extract_and_store(self, updater):
        ids = await updater.extract_and_store([
            {"role": "user", "content": "I prefer dark mode"},
        ])
        assert len(ids) == 2

    @pytest.mark.asyncio
    async def test_dedup_prevents_duplicates(self, updater):
        # First call stores
        ids1 = await updater.extract_and_store([{"role": "user", "content": "dark mode please"}])
        # Second call with same content should be deduped
        ids2 = await updater.extract_and_store([{"role": "user", "content": "dark mode please"}])
        assert len(ids2) == 0  # nothing new

    @pytest.mark.asyncio
    async def test_filters_low_importance(self, updater):
        # This provider returns only low-importance items
        low_provider = _FakeProvider(
            '[{"content":"trivial detail","category":"other","importance":0.1}]'
        )
        ltm = updater._ltm
        low_updater = MemoryUpdater(ltm, MemorySummarizer(low_provider), min_importance=0.4)
        ids = await low_updater.extract_and_store([{"role": "user", "content": "hi"}])
        assert ids == []
