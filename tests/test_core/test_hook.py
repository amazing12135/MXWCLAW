"""测试 core/hook.py"""

import pytest

from mxwbot.core.hook import StreamProcessHook
from mxwbot.providers.base import LLMResponse, TokenUsage


class TestStreamProcessHook:
    def test_accumulates_text(self):
        hook = StreamProcessHook(clean_thinking=False)
        pytest.importorskip("asyncio")
        import asyncio
        asyncio.run(hook.on_stream_delta("hello "))
        asyncio.run(hook.on_stream_delta("world"))
        assert hook.full_text == "hello world"
        assert hook.cleaned_text == "hello world"

    def test_cleans_think_tags(self):
        hook = StreamProcessHook(clean_thinking=True)
        import asyncio
        asyncio.run(hook.on_stream_delta("<think>reasoning</think>"))
        asyncio.run(hook.on_stream_delta("actual answer"))
        assert "<think>" not in hook.cleaned_text
        assert "actual answer" in hook.cleaned_text

    def test_reset(self):
        hook = StreamProcessHook(clean_thinking=False)
        import asyncio
        asyncio.run(hook.on_stream_delta("hello"))
        hook.reset()
        assert hook.full_text == ""
        assert hook.cleaned_text == ""

    def test_on_llm_response_noop(self):
        hook = StreamProcessHook()
        import asyncio
        asyncio.run(hook.on_llm_response(
            LLMResponse(content="ok", usage=TokenUsage()))
        )
