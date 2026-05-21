"""测试 core/loop.py — LoopPool + Loop 编排器。

覆盖场景:
  * 去重丢弃
  * /clear /stop /status 命令短路
  * 正常完整 turn
  * Runner 错误恢复
  * 确认响应路由
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mxwbot.bus.messages import InboundMessage, OutboundMessage
from mxwbot.bus.queue import MessageBus
from mxwbot.checkpoint.manager import CheckpointManager
from mxwbot.core.context import ContextBuilder
from mxwbot.core.loop import Loop, LoopContext, LoopPool
from mxwbot.core.runner import AgentRunner, AgentRunSpec, AgentRunResult
from mxwbot.core.skill import SkillLoader
from mxwbot.core.state import StateManager, TurnState
from mxwbot.core.tools.base import Tool, ToolResult
from mxwbot.core.tools.register import ToolRegistry
from mxwbot.memory.core import MemoryManager
from mxwbot.providers.base import LLMCallPurpose, LLMProvider, LLMResponse, TokenUsage, ToolCallRequest
from mxwbot.session.manager import SessionManager


# ---------------------------------------------------------------------------
# Fake / mock helpers
# ---------------------------------------------------------------------------

class _FakeProvider(LLMProvider):
    """模拟 LLM Provider。"""

    def __init__(self, responses=None):
        super().__init__(model="test")
        self._responses = list(responses or [])
        self._idx = 0

    @property
    def supports_streaming(self) -> bool:
        return True

    async def _chat_impl(self, messages, tools):
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return LLMResponse(
            content="done",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )

    async def _chat_stream_impl(self, messages, tools):
        if False:
            yield
        return


class _FakeRunner(AgentRunner):
    """可预设结果的 AgentRunner。"""

    def __init__(self, result=None):
        super().__init__()
        self._result = result or AgentRunResult(content="hello", finish_reason="stop")

    async def run(self, spec, messages):
        return self._result

    async def run_stream(self, spec, messages):
        # Publish any pre-loaded content as stream deltas
        if self._result.content and spec.hook:
            for word in self._result.content.split():
                await spec.hook.on_stream_delta(word + " ")
        return self._result


def _make_inbound(
    channel="test", chat_id="u1",
    content="hello", msg_type="message", idempotency_key="",
    **kwargs,
) -> InboundMessage:
    return InboundMessage(
        channel=channel, chat_id=chat_id, content=content,
        msg_type=msg_type, idempotency_key=(
            idempotency_key or f"{channel}:{chat_id}:m1"
        ),
        **kwargs,
    )


async def _make_loop_pool(
    bus=None,
    session_manager=None,
    memory_manager=None,
    context_builder=None,
    runner=None,
    provider=None,
    tools=None,
    checkpoint=None,
    skill_loader=None,
    *,
    tmp_workspace=None,
) -> LoopPool:
    ws = tmp_workspace or Path(".")
    if bus is None:
        bus = MessageBus()
    if session_manager is None:
        session_manager = SessionManager(ws)
    if provider is None:
        provider = _FakeProvider()
    if memory_manager is None:
        memory_manager = MemoryManager(ws, provider)
        await memory_manager.init_db()
    if context_builder is None:
        context_builder = ContextBuilder(ws)
    if runner is None:
        runner = _FakeRunner()
    if tools is None:
        tools = ToolRegistry()
    if checkpoint is None:
        checkpoint = CheckpointManager(ws)
    if skill_loader is None:
        skill_loader = SkillLoader()

    return LoopPool(
        bus=bus,
        sessions=session_manager,
        memory=memory_manager,
        context_builder=context_builder,
        runner=runner,
        provider=provider,
        tools=tools,
        checkpoint=checkpoint,
        skills=skill_loader,
    )


# ---------------------------------------------------------------------------
# Test: LoopContext
# ---------------------------------------------------------------------------

class TestLoopContext:
    def test_defaults(self):
        session_mgr = SessionManager(Path("."))
        bus = MessageBus()
        msg = _make_inbound()
        session = asyncio.run(session_mgr.get_session("test", "u1"))

        ctx = LoopContext(msg=msg, session=session, session_key="test:u1")
        assert ctx.msg is msg
        assert ctx.session is session
        assert ctx.messages == []
        assert ctx.result is None
        assert ctx.summary == ""
        assert ctx.checkpoint_restored is False


# ---------------------------------------------------------------------------
# Test: Loop state handlers
# ---------------------------------------------------------------------------

class TestLoopHandlers:
    async def _make_loop(self, **overrides):
        ws = Path(".")
        bus = MessageBus()
        sessions = SessionManager(ws)
        provider = _FakeProvider()
        memory = MemoryManager(ws, provider)
        await memory.init_db()
        ctx_builder = ContextBuilder(ws)
        runner = _FakeRunner()
        tools = ToolRegistry()
        checkpoint = CheckpointManager(ws)
        skills = SkillLoader()

        msg = _make_inbound()
        session = await sessions.get_session("test", "u1")
        ctx = LoopContext(msg=msg, session=session, session_key="test:u1")

        kwargs = dict(
            ctx=ctx, sessions=sessions, memory=memory,
            context_builder=ctx_builder, runner=runner,
            provider=provider, tools=tools, checkpoint=checkpoint,
            skills=skills, bus=bus,
        )
        kwargs.update(overrides)
        return Loop(**kwargs)

    @pytest.mark.asyncio
    async def test_command_dispatch_normal(self):
        """普通消息 → dispatch 到 RESTORE。"""
        loop = await self._make_loop()
        loop.ctx.msg.content = "hello"
        event = await loop._handle_command()
        assert event == "dispatch"

    @pytest.mark.asyncio
    async def test_command_shortcut_clear(self):
        """/clear 命令 → shortcut 到 SAVE。"""
        loop = await self._make_loop()
        loop.ctx.msg.content = "/clear"
        event = await loop._handle_command()
        assert event == "shortcut"
        assert "已清空" in loop.ctx.result.content

    @pytest.mark.asyncio
    async def test_command_shortcut_stop(self):
        """/stop 命令 → shortcut。"""
        loop = await self._make_loop()
        loop.ctx.msg.content = "/stop"
        event = await loop._handle_command()
        assert event == "shortcut"

    @pytest.mark.asyncio
    async def test_command_shortcut_status(self):
        """/status 命令 → shortcut。"""
        loop = await self._make_loop()
        loop.ctx.msg.content = "/status"
        event = await loop._handle_command()
        assert event == "shortcut"
        assert "会话状态" in loop.ctx.result.content

    @pytest.mark.asyncio
    async def test_restore_no_checkpoint(self):
        """无 checkpoint 时正常返回 ok。"""
        loop = await self._make_loop()
        event = await loop._handle_restore()
        assert event == "ok"
        assert loop.ctx.checkpoint_restored is False

    @pytest.mark.asyncio
    async def test_compact_below_threshold(self):
        """消息数少时跳过 compact。"""
        loop = await self._make_loop()
        event = await loop._handle_compact()
        assert event == "ok"

    @pytest.mark.asyncio
    async def test_build_assembles_messages(self):
        """BUILD 状态组装消息列表。"""
        loop = await self._make_loop()
        loop.ctx.msg.content = "test query"
        event = await loop._handle_build()
        assert event == "ok"
        assert len(loop.ctx.messages) >= 2  # system + user
        assert any(m["role"] == "user" for m in loop.ctx.messages)

    @pytest.mark.asyncio
    async def test_run_returns_ok(self):
        """正常 RUN → ok。"""
        loop = await self._make_loop()
        loop._runner = _FakeRunner(
            AgentRunResult(content="response", finish_reason="stop")
        )
        event = await loop._handle_run()
        assert event == "ok"
        assert loop.ctx.result.content == "response"

    @pytest.mark.asyncio
    async def test_run_error_returns_error(self):
        """Runner 错误 → error。"""
        loop = await self._make_loop()
        loop._runner = _FakeRunner(
            AgentRunResult(content="", finish_reason="error")
        )
        event = await loop._handle_run()
        assert event == "error"

    @pytest.mark.asyncio
    async def test_save_persists_message(self):
        """SAVE 持久化入站消息。"""
        loop = await self._make_loop()
        loop.ctx.result = AgentRunResult(content="reply", finish_reason="stop")
        event = await loop._handle_save()
        assert event == "ok"
        assert loop.ctx.session.message_count >= 1

    @pytest.mark.asyncio
    async def test_respond_publishes_outbound(self):
        """RESPOND 发布 OutboundMessage 到 Bus。"""
        loop = await self._make_loop()
        loop.ctx.result = AgentRunResult(content="reply", finish_reason="stop")

        event = await loop._handle_respond()
        assert event == "ok"

        # 验证 bus 上有输出消息
        out_queue = await loop._bus.subscribe("test")
        msg = await out_queue.get()
        assert msg.content == "reply"
        assert msg.channel == "test"
        assert msg.chat_id == "u1"

    @pytest.mark.asyncio
    async def test_state_machine_full_cycle(self):
        """完整状态机走通：COMMAND → ... → DONE。"""
        ws = Path(".")
        bus = MessageBus()
        sessions = SessionManager(ws)
        provider = _FakeProvider()
        memory = MemoryManager(ws, provider)
        ctx_builder = ContextBuilder(ws)
        runner = _FakeRunner(
            AgentRunResult(content="processed", finish_reason="stop")
        )
        tools = ToolRegistry()
        checkpoint = CheckpointManager(ws)
        skills = SkillLoader()

        msg = _make_inbound(content="hi")
        session = await sessions.get_session("test", "u1")
        ctx = LoopContext(msg=msg, session=session, session_key="test:u1")

        loop = Loop(
            ctx=ctx, sessions=sessions, memory=memory,
            context_builder=ctx_builder, runner=runner,
            provider=provider, tools=tools, checkpoint=checkpoint,
            skills=skills, bus=bus,
        )
        await loop.run()

        # 验证结果
        assert ctx.result is not None
        assert ctx.result.content == "processed"

        # 验证消息已发布到 bus
        out_queue = await bus.subscribe("test")
        out = await out_queue.get()
        assert out.content == "processed"


# ---------------------------------------------------------------------------
# Test: LoopPool
# ---------------------------------------------------------------------------

class TestLoopPool:
    @pytest.mark.asyncio
    async def test_dispatch_normal_message(self, tmp_workspace):
        """普通消息 → 完整处理 → 回复发布。"""
        ws = tmp_workspace
        bus = MessageBus()
        pool = await _make_loop_pool(bus=bus, tmp_workspace=ws)

        # 发布一条入站消息
        msg = _make_inbound(content="hello world")
        await bus.publish_inbound(msg)

        # 手动 dispatch（不启动消费循环）
        await pool._dispatch(msg)

        # 验证回复已发布到 outbound
        out_queue = await bus.subscribe("test")
        out = await out_queue.get()
        assert out.content == "hello"
        assert out.msg_type == "message"

    @pytest.mark.asyncio
    async def test_duplicate_dropped(self, tmp_workspace):
        """重复消息被去重丢弃。"""
        ws = tmp_workspace
        bus = MessageBus()
        pool = await _make_loop_pool(bus=bus, tmp_workspace=ws)

        msg = _make_inbound(content="first", idempotency_key="test:u1:m99")
        # 先标记为已见
        pool._sessions.mark_seen(msg)

        # 发布到队列供 subscribe 读取（验证无输出）
        await bus.publish_inbound(msg)

        # 手动 dispatch → 应该被去重跳过
        await pool._dispatch(msg)

        # 确认 outbound 队列中没有新消息
        out_queue = await bus.subscribe("test")
        assert out_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_confirmation_response_routed(self, tmp_workspace):
        """confirmation_response 消息唤醒挂起的事件。"""
        bus = MessageBus()
        # 模拟一个挂起的确认请求
        event = asyncio.Event()
        bus._pending["req-1"] = event

        pool = await _make_loop_pool(bus=bus, tmp_workspace=tmp_workspace)

        # 发布确认响应
        msg = InboundMessage(
            channel="test", chat_id="u1",
            content="approved",
            msg_type="confirmation_response",
            ref_request_id="req-1",
        )
        await bus.publish_inbound(msg)

        # 消费
        consumed = await bus.input_queue.get()
        pool._resolve_confirmation(consumed)

        # 事件应该被设置
        assert event.is_set()
        assert bus._results.get("req-1") is True


# ---------------------------------------------------------------------------
# Test: State transitions
# ---------------------------------------------------------------------------

class TestTurnStateTransitions:
    def test_all_events_reachable(self):
        """验证转移表中每个状态都能推进。"""
        sm = StateManager()
        # COMMAND → dispatch → RESTORE
        assert sm.current == TurnState.COMMAND
        sm.dispatch("dispatch")
        assert sm.current == TurnState.RESTORE

        # RESTORE → ok → COMPACT
        sm.dispatch("ok")
        assert sm.current == TurnState.COMPACT

        # COMPACT → ok → BUILD
        sm.dispatch("ok")
        assert sm.current == TurnState.BUILD

        # BUILD → ok → RUN
        sm.dispatch("ok")
        assert sm.current == TurnState.RUN

        # RUN → ok → SAVE
        sm.dispatch("ok")
        assert sm.current == TurnState.SAVE

        # SAVE → ok → RESPOND
        sm.dispatch("ok")
        assert sm.current == TurnState.RESPOND

        # RESPOND → ok → DONE
        sm.dispatch("ok")
        assert sm.current == TurnState.DONE
        assert sm.is_terminal

    def test_command_shortcut_skips_to_save(self):
        """COMMAND → shortcut → 直接到 SAVE。"""
        sm = StateManager()
        sm.dispatch("shortcut")
        assert sm.current == TurnState.SAVE

    def test_run_error_back_to_restore(self):
        """RUN → error → 回退到 RESTORE。"""
        sm = StateManager()
        # 快速走到 RUN
        sm.dispatch("dispatch")  # COMMAND → RESTORE
        sm.dispatch("ok")        # RESTORE → COMPACT
        sm.dispatch("ok")        # COMPACT → BUILD
        sm.dispatch("ok")        # BUILD → RUN
        assert sm.current == TurnState.RUN

        sm.dispatch("error")
        assert sm.current == TurnState.RESTORE
