"""Tests for session/manager.py — SessionManager."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mxwbot.bus.messages import InboundMessage
from mxwbot.session.manager import SessionManager


class TestSessionManager:
    @pytest.fixture
    def sm(self, tmp_path) -> SessionManager:
        return SessionManager(tmp_path / ".mxwbot")

    def _msg(self, channel="wechat", chat_id="u1", content="hi",
             idempotency_key="wx:u1:001") -> InboundMessage:
        return InboundMessage(
            channel=channel, chat_id=chat_id, content=content,
            idempotency_key=idempotency_key,
        )

    @pytest.mark.asyncio
    async def test_get_session_creates_new(self, sm):
        session = await sm.get_session("wechat", "u1")
        assert session.channel == "wechat"
        assert session.chat_id == "u1"
        assert session.message_count == 0

    @pytest.mark.asyncio
    async def test_get_session_returns_cached(self, sm):
        s1 = await sm.get_session("wechat", "u1")
        s2 = await sm.get_session("wechat", "u1")
        assert s1 is s2

    @pytest.mark.asyncio
    async def test_save_and_reload(self, sm):
        msg = self._msg(content="hello")
        session = await sm.get_session("wechat", "u1")
        await sm.save_inbound(session, msg)

        # Load from disk via a new SessionManager
        sm2 = SessionManager(sm._workspace)
        s2 = await sm2.get_session("wechat", "u1")
        assert s2.message_count == 1
        assert s2.messages[0]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_dedup(self, sm):
        msg1 = self._msg(idempotency_key="wx:u1:001")
        msg2 = self._msg(idempotency_key="wx:u1:001")  # duplicate

        assert sm.is_duplicate(msg1) is False
        sm.mark_seen(msg1)
        assert sm.is_duplicate(msg2) is True

    @pytest.mark.asyncio
    async def test_dedup_different_keys(self, sm):
        msg1 = self._msg(idempotency_key="wx:u1:001")
        msg2 = self._msg(idempotency_key="wx:u1:002")

        sm.mark_seen(msg1)
        assert sm.is_duplicate(msg2) is False

    @pytest.mark.asyncio
    async def test_compact(self, sm):
        """Compact keeps only recent messages."""
        msg = self._msg(content="m")
        session = await sm.get_session("wechat", "u1")

        # Add 20 messages
        for i in range(20):
            session.messages.append({"content": f"msg-{i}"})

        # Force compact keeping 5
        await sm.compact(session, keep_last=5)
        assert len(session.messages) == 5
        assert session.messages[-1]["content"] == "msg-19"

    @pytest.mark.asyncio
    async def test_compact_atomic(self, sm):
        """Compact uses tmp + rename, does not corrupt existing data."""
        msg = self._msg(content="persisted")
        session = await sm.get_session("wechat", "u1")
        await sm.save_inbound(session, msg)

        # Add many in-memory messages
        for i in range(10):
            session.messages.append({"content": f"msg-{i}"})

        await sm.compact(session, keep_last=3)

        # Reload — file should have 3 messages intact
        sm2 = SessionManager(sm._workspace)
        s2 = await sm2.get_session("wechat", "u1")
        assert s2.message_count == 3

    @pytest.mark.asyncio
    async def test_lock_is_reusable(self, sm):
        lock1 = sm.get_lock("wechat", "u1")
        lock2 = sm.get_lock("wechat", "u1")
        assert lock1 is lock2

        lock3 = sm.get_lock("wechat", "u2")
        assert lock1 is not lock3

    @pytest.mark.asyncio
    async def test_repair_corrupted_file(self, sm):
        """A corrupted line should be skipped, not cause full loss."""
        from mxwbot.config.path import get_session_path
        path = get_session_path(sm._workspace, "wechat", "u1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"role": "user", "content": "hello"}\n'
            'NOT VALID JSON\n'
            '{"role": "assistant", "content": "hi"}\n',
            encoding="utf-8",
        )
        s = await sm.get_session("wechat", "u1")
        assert s.message_count == 2
        assert s.messages[0]["content"] == "hello"
        assert s.messages[1]["content"] == "hi"

    @pytest.mark.asyncio
    async def test_save_write_then_cache_order(self, sm):
        """After a successful save, memory should reflect the written data."""
        msg = self._msg(content="persisted")
        session = await sm.get_session("wechat", "u1")

        # Save should succeed (normal case)
        await sm.save_inbound(session, msg)

        # Memory must match
        assert session.message_count == 1
        assert session.messages[0]["content"] == "persisted"

        # File must also match
        from mxwbot.config.path import get_session_path
        path = get_session_path(sm._workspace, "wechat", "u1")
        content = path.read_text(encoding="utf-8")
        assert "persisted" in content

    @pytest.mark.asyncio
    async def test_get_recent(self, sm):
        msg1 = self._msg(content="msg-1")
        msg2 = self._msg(content="msg-2")
        msg3 = self._msg(content="msg-3")
        session = await sm.get_session("wechat", "u1")
        await sm.save_inbound(session, msg1)
        await sm.save_inbound(session, msg2)
        await sm.save_inbound(session, msg3)

        recent = session.get_recent(2)
        assert len(recent) == 2
        assert recent[-1]["content"] == "msg-3"

    @pytest.mark.asyncio
    async def test_get_history_unconsolidated(self, sm):
        """get_history 只返回未压缩部分 + 做 user turn 对齐。"""
        session = await sm.get_session("wechat", "u1")
        # 手动填充消息
        for i in range(5):
            session.messages.append({"role": "user", "content": f"msg-{i}"})
            session.messages.append({"role": "assistant", "content": f"reply-{i}"})
        session.consolidated_count = 4  # 前两条对话已压缩

        history = session.get_history()
        assert len(history) == 6  # 10 total - 4 consolidated = 6

    @pytest.mark.asyncio
    async def test_get_history_aligns_to_user(self, sm):
        """get_history 从第一个 user 消息开始，不会从 assistant 开头。"""
        session = await sm.get_session("wechat", "u1")
        session.messages = [
            {"role": "assistant", "content": "orphan reply"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        history = session.get_history()
        assert history[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_get_history_removes_orphan_tools(self, sm):
        """get_history 移除前导孤立 tool result。"""
        session = await sm.get_session("wechat", "u1")
        session.messages = [
            {"role": "tool", "tool_call_id": "no-such-call", "content": "orphan"},
            {"role": "user", "content": "hi"},
        ]
        history = session.get_history()
        assert len(history) == 1
        assert history[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_compact_structural_alignment(self, sm):
        """compact 截断前对齐 user turn。"""
        session = await sm.get_session("wechat", "u1")
        for i in range(10):
            session.messages.append({"role": "user", "content": f"q-{i}"})
            session.messages.append({"role": "assistant", "content": f"a-{i}"})

        await sm.compact(session, keep_last=5)
        # 保留的 5 条应该是从某个 user turn 开始
        assert session.messages[0]["role"] == "user"
        assert len(session.messages) <= 5

    @pytest.mark.asyncio
    async def test_clear_resets_state(self, sm):
        msg = self._msg(content="hello")
        session = await sm.get_session("wechat", "u1")
        await sm.save_inbound(session, msg)
        session.active_task = "doing something"
        session.consolidated_count = 5

        session.clear()

        assert session.message_count == 0
        assert session.active_task is None
        assert session.consolidated_count == 0

    @pytest.mark.asyncio
    async def test_append_assistant_message(self, sm):
        """Session 可以直接追加 assistant / tool 消息（不只是 InboundMessage）。"""
        session = await sm.get_session("wechat", "u1")
        await session.append_message({"role": "user", "content": "hi"})
        await session.append_message({"role": "assistant", "content": "hello!"})

        assert session.message_count == 2
        assert session.messages[1]["role"] == "assistant"
        assert session.messages[1]["content"] == "hello!"

    @pytest.mark.asyncio
    async def test_save_inbound_format(self, sm):
        """save_inbound 将 InboundMessage 转为标准 role/content 格式。"""
        session = await sm.get_session("wechat", "u1")
        msg = self._msg(content="hello user")
        await sm.save_inbound(session, msg)

        assert session.messages[0]["role"] == "user"
        assert session.messages[0]["content"] == "hello user"
        assert "channel" not in session.messages[0]  # InboundMessage meta stripped
        assert session.messages[0]["msg_id"] == msg.id

    @pytest.mark.asyncio
    async def test_close_clears_and_removes(self, sm):
        session = await sm.get_session("wechat", "u1")
        assert "wechat:u1" in sm._sessions

        await sm.close(session)
        assert "wechat:u1" not in sm._sessions
        assert "wechat:u1" not in sm._locks
        assert session.message_count == 0          # cleared
        assert session.active_task is None

    @pytest.mark.asyncio
    async def test_close_reload_works(self, sm):
        msg = self._msg(content="saved-before-close")
        session = await sm.get_session("wechat", "u1")
        await sm.save_inbound(session, msg)
        await sm.close(session)

        # Re-access — should reload from JSONL
        s2 = await sm.get_session("wechat", "u1")
        assert s2.message_count == 1
        assert s2.messages[0]["content"] == "saved-before-close"
