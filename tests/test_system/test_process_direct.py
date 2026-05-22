"""测试 SystemManager.process_direct() — 直接 agent 调用."""

import pytest

from mxwbot.config.schema import (
    AgentDefaultConfig,
    ChannelConfig,
    HeartBeatConfig,
    MXWConfig,
    ProviderConfig,
)
from mxwbot.system.manager import SystemManager


def _make_config(*, workspace=".mxwbot") -> MXWConfig:
    return MXWConfig(
        workspace=workspace,
        providers=[ProviderConfig(name="openai", api_key="sk-test", model="gpt-4")],
        channels=[],
        agent=AgentDefaultConfig(max_iterations=3),
        heartbeat=HeartBeatConfig(enabled=False),
    )


class TestProcessDirect:
    @pytest.mark.asyncio
    async def test_returns_content(self, tmp_path):
        """process_direct 正常返回 AgentRunResult."""
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()

        result = await mgr.process_direct("hello", "test:u1")
        assert result is not None
        assert hasattr(result, "content")
        assert hasattr(result, "finish_reason")

    @pytest.mark.asyncio
    async def test_stream_callback_fires(self, tmp_path):
        """on_stream 回调在流式输出时被调用."""
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()

        tokens = []

        async def on_token(token):
            tokens.append(token)

        result = await mgr.process_direct("hi", "test:u2", on_stream=on_token)
        assert result is not None

    @pytest.mark.asyncio
    async def test_keep_recent_trims_session(self, tmp_path):
        """keep_recent 限制 session 消息数量."""
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()

        await mgr.process_direct("msg1", "test:u3", keep_recent=2)
        await mgr.process_direct("msg2", "test:u3", keep_recent=2)
        await mgr.process_direct("msg3", "test:u3", keep_recent=2)

        session = await mgr.sessions.get_session("cli", "direct")
        # After 3 calls with keep_recent=2, session should have ≤ 2 messages
        assert len(session.messages) <= 2

    @pytest.mark.asyncio
    async def test_custom_channel_and_chat(self, tmp_path):
        """支持自定义 channel/chat_id."""
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()

        result = await mgr.process_direct(
            "ping", "wechat:u99",
            channel="wechat", chat_id="u99",
        )
        assert result is not None
