"""Tests for memory/summarizer.py."""

import pytest

from mxwbot.memory.summarizer import MemorySummarizer
from mxwbot.providers.base import LLMProvider, LLMResponse
from mxwbot.providers.registry import ProviderRegistry


class FakeSummarizerProvider(LLMProvider):
    """Returns structured extraction JSON."""

    @property
    def supports_streaming(self) -> bool:
        return True

    async def _chat_impl(self, messages, tools):
        content = messages[-1]["content"]
        if "Summarise" in content:
            return LLMResponse(content="User talked about testing. Decisions: use pytest.")
        # extract_facts prompt
        return LLMResponse(
            content='[{"content":"User prefers dark mode","category":"preference","importance":0.8},'
                    '{"content":"Uses pytest","category":"project_info","importance":0.6}]'
        )

    async def _chat_stream_impl(self, messages, tools):
        if False:
            yield
        return


@pytest.fixture
def provider():
    return FakeSummarizerProvider(model="test")


class TestMemorySummarizer:
    @pytest.mark.asyncio
    async def test_extract_facts(self, provider):
        s = MemorySummarizer(provider)
        facts = await s.extract_facts([
            {"role": "user", "content": "I prefer dark mode"},
        ])
        assert len(facts) == 2
        assert facts[0]["content"] == "User prefers dark mode"
        assert facts[0]["importance"] == 0.8

    @pytest.mark.asyncio
    async def test_extract_facts_empty_messages(self, provider):
        s = MemorySummarizer(provider)
        facts = await s.extract_facts([])
        assert facts == []

    @pytest.mark.asyncio
    async def test_summarise_session(self, provider):
        s = MemorySummarizer(provider)
        summary = await s.summarise_session([
            {"role": "user", "content": "Let's use pytest"},
        ])
        assert "pytest" in summary

    @pytest.mark.asyncio
    async def test_parse_json_with_fences(self, provider):
        """Some models wrap JSON in ``` fences."""
        s = MemorySummarizer(provider)
        # Override provider response for this test
        async def fenced_response(messages, tools):
            from mxwbot.providers.base import LLMResponse
            return LLMResponse(
                content='```json\n[{"content":"test","category":"fact","importance":0.5}]\n```'
            )
        provider._chat_impl = fenced_response
        facts = await s.extract_facts([{"role": "user", "content": "test"}])
        assert len(facts) == 1
