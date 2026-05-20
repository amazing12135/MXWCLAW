"""测试 BaseChannel 模板方法流程。

使用 Mock Channel 子类验证确认拦截、TTL 超时、流式处理等模板方法行为。
"""

import asyncio
from typing import Any

import pytest

from mxwbot.channel.base import (
    BaseChannel,
    ChannelAuthError,
    ChannelError,
    ChannelFatalError,
    ChannelTransientError,
)
from mxwbot.bus.messages import InboundMessage, OutboundMessage, StreamDelta
from mxwbot.bus.queue import MessageBus


# ---------------------------------------------------------------------------
# Mock Channel — 实现 4 个抽象方法用于测试模板方法
# ---------------------------------------------------------------------------

class _MockChannel(BaseChannel):
    """Mock 频道：所有 I/O 方法记录调用但不执行实际操作。"""

    name = "mock"

    def __init__(self, bus: MessageBus, *, supports_buttons: bool = False):
        super().__init__(bus)
        self._supports_buttons = supports_buttons
        # 记录调用
        self.started = False
        self.stopped = False
        self.sent_texts: list[tuple[str, str]] = []  # (chat_id, text)
        self.sent_chunks: list[tuple[str, str]] = []
        # 可配置的 _parse 行为
        self._parse_return: dict | None = None
        self.rendered_buttons: list[OutboundMessage] = []

    async def _start(self) -> None:
        self.started = True

    async def _stop(self) -> None:
        self.stopped = True

    async def _send_text(self, chat_id: str, text: str) -> None:
        self.sent_texts.append((chat_id, text))

    def _parse(self, raw_msg: Any) -> dict | None:
        return self._parse_return

    def supports_interactive_buttons(self) -> bool:
        return self._supports_buttons

    async def _render_confirmation_buttons(self, msg: OutboundMessage) -> None:
        self.rendered_buttons.append(msg)
        await self._send_text(msg.chat_id, f"[BUTTON] {msg.content}")


# ---------------------------------------------------------------------------
# 基础生命周期
# ---------------------------------------------------------------------------

class TestChannelLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self):
        bus = MessageBus()
        ch = _MockChannel(bus)
        await ch.start()
        assert ch.started is True
        assert ch._running is True

        await ch.stop()
        assert ch.stopped is True
        assert ch._running is False

    @pytest.mark.asyncio
    async def test_pending_cleared_on_stop(self):
        bus = MessageBus()
        ch = _MockChannel(bus)
        ch._pending_confirmations["u1"] = ["req-1"]
        await ch.stop()
        assert "u1" not in ch._pending_confirmations


# ---------------------------------------------------------------------------
# 发送消息
# ---------------------------------------------------------------------------

class TestChannelSend:
    @pytest.mark.asyncio
    async def test_send_normal_message(self):
        bus = MessageBus()
        ch = _MockChannel(bus)
        msg = OutboundMessage(channel="mock", chat_id="u1", content="hello")
        await ch.send(msg)
        assert ch.sent_texts == [("u1", "hello")]

    @pytest.mark.asyncio
    async def test_send_confirmation_request_fallback(self):
        """不支持按钮的频道 → 发送 fallback 提示文本。"""
        bus = MessageBus()
        ch = _MockChannel(bus, supports_buttons=False)
        msg = OutboundMessage(
            channel="mock", chat_id="u1",
            msg_type="confirmation_request",
            request_id="req-1",
            content="即将执行 1 个写操作",
            timeout_seconds=5,
        )
        await ch.send(msg)
        assert "u1" in ch._pending_confirmations
        assert ch._pending_confirmations["u1"] == ["req-1"]
        assert ch.sent_texts[0][0] == "u1"

    @pytest.mark.asyncio
    async def test_send_confirmation_request_with_buttons(self):
        """支持按钮的频道 → 渲染按钮。"""
        bus = MessageBus()
        ch = _MockChannel(bus, supports_buttons=True)
        msg = OutboundMessage(
            channel="mock", chat_id="u1",
            msg_type="confirmation_request",
            request_id="req-1",
            content="确认写操作",
            timeout_seconds=5,
        )
        await ch.send(msg)
        assert len(ch.rendered_buttons) == 1
        assert ch.rendered_buttons[0].request_id == "req-1"


# ---------------------------------------------------------------------------
# 确认拦截
# ---------------------------------------------------------------------------

class TestConfirmationInterception:
    @pytest.mark.asyncio
    async def test_user_approves(self):
        bus = MessageBus()
        ch = _MockChannel(bus)
        # 模拟 pending 状态
        ch._pending_confirmations["u1"] = ["req-1"]
        ch._parse_return = {
            "chat_id": "u1", "content": "Y", "platform_msg_id": "m1",
        }

        await ch.on_message({"raw": "msg"})

        # 应该投递 confirmation_response 到 Bus
        inbound = await bus.input_queue.get()
        assert inbound.msg_type == "confirmation_response"
        assert inbound.content == "approved"
        assert inbound.ref_request_id == "req-1"
        assert "u1" not in ch._pending_confirmations

    @pytest.mark.asyncio
    async def test_user_denies(self):
        bus = MessageBus()
        ch = _MockChannel(bus)
        ch._pending_confirmations["u1"] = ["req-1"]
        ch._parse_return = {
            "chat_id": "u1", "content": "N", "platform_msg_id": "m1",
        }

        await ch.on_message({"raw": "msg"})

        inbound = await bus.input_queue.get()
        assert inbound.content == "denied"

    @pytest.mark.asyncio
    async def test_chinese_approve(self):
        """中文回复"是""确认""同意"视为批准。"""
        bus = MessageBus()
        ch = _MockChannel(bus)
        for answer in ("是", "确认", "同意"):
            ch._pending_confirmations["u1"] = [f"req-{answer}"]
            ch._parse_return = {
                "chat_id": "u1", "content": answer, "platform_msg_id": "m1",
            }
            await ch.on_message({"raw": "msg"})
            inbound = await bus.input_queue.get()
            assert inbound.content == "approved", f"'{answer}' should be approved"

    @pytest.mark.asyncio
    async def test_parse_none_silently_dropped(self):
        """_parse 返回 None → 不投递任何消息。"""
        bus = MessageBus()
        ch = _MockChannel(bus)
        ch._parse_return = None

        await ch.on_message({"raw": "bad"})
        assert bus.input_queue.qsize() == 0


# ---------------------------------------------------------------------------
# TTL 超时
# ---------------------------------------------------------------------------

class TestConfirmationTTL:
    @pytest.mark.asyncio
    async def test_ttl_expiry_restores_normal_flow(self):
        """TTL 超时后，用户回复不再是确认回复，而是正常消息。"""
        bus = MessageBus()
        ch = _MockChannel(bus)
        ch._pending_confirmations["u1"] = ["req-1"]

        # TTL 短超时 → 等待过期
        ch._schedule_confirmation_ttl("u1", "req-1", 0.05)
        await asyncio.sleep(0.1)

        # pending 已被清除
        assert "u1" not in ch._pending_confirmations

        # 用户回复应走正常消息路径
        ch._parse_return = {
            "chat_id": "u1", "content": "hello again", "platform_msg_id": "m2",
        }
        await ch.on_message({"raw": "msg"})

        inbound = await bus.input_queue.get()
        assert inbound.msg_type == "message"
        assert inbound.content == "hello again"


# ---------------------------------------------------------------------------
# 正常消息投递
# ---------------------------------------------------------------------------

class TestNormalMessage:
    @pytest.mark.asyncio
    async def test_publishes_inbound_message(self):
        bus = MessageBus()
        ch = _MockChannel(bus)
        ch._parse_return = {
            "chat_id": "u1", "content": "hello world", "platform_msg_id": "m99",
        }
        await ch.on_message({"raw": "test"})

        inbound = await bus.input_queue.get()
        assert inbound.channel == "mock"
        assert inbound.chat_id == "u1"
        assert inbound.content == "hello world"
        assert inbound.msg_type == "message"
        assert inbound.idempotency_key == "mock:u1:m99"

    @pytest.mark.asyncio
    async def test_no_platform_msg_id(self):
        """无 platform_msg_id 时 idempotency_key 由 InboundMessage.__post_init__ 自动生成。"""
        bus = MessageBus()
        ch = _MockChannel(bus)
        ch._parse_return = {"chat_id": "u1", "content": "hi"}
        await ch.on_message({"raw": "test"})

        inbound = await bus.input_queue.get()
        # __post_init__ 在 idempotency_key 为空时自动生成 f"{channel}:{chat_id}:{id}"
        assert inbound.channel == "mock"
        assert "mock:u1:" in inbound.idempotency_key


# ---------------------------------------------------------------------------
# 流式处理
# ---------------------------------------------------------------------------

class TestStreamSending:
    @pytest.mark.asyncio
    async def test_non_streaming_buffers_and_sends(self):
        """不支持流式的频道 → 累积所有 delta → 一次性发送。"""
        bus = MessageBus()
        ch = _MockChannel(bus)
        assert ch.supports_streaming() is False

        # 启动流式消费（后台）
        task = asyncio.create_task(ch.send_stream("u1"))

        # 发送 delta
        await bus.publish_stream_delta(StreamDelta(
            stream_id="s1", channel="mock", chat_id="u1", delta="hello ", seq=1,
        ))
        await bus.publish_stream_delta(StreamDelta(
            stream_id="s1", channel="mock", chat_id="u1", delta="world", seq=2,
        ))
        await bus.publish_stream_delta(StreamDelta(
            stream_id="s1", channel="mock", chat_id="u1", delta="", seq=3, is_end=True,
        ))

        await asyncio.wait_for(task, timeout=1)

        assert len(ch.sent_texts) == 1
        assert ch.sent_texts[0] == ("u1", "hello world")

    @pytest.mark.asyncio
    async def test_streaming_sends_chunks(self):
        """支持流式的频道 → 逐块发送。"""
        bus = MessageBus()
        ch = _MockChannel(bus)
        ch.supports_streaming = lambda: True  # 覆写为支持流式

        task = asyncio.create_task(ch.send_stream("u1"))

        await bus.publish_stream_delta(StreamDelta(
            stream_id="s1", channel="mock", chat_id="u1", delta="chunk1", seq=1,
        ))
        await bus.publish_stream_delta(StreamDelta(
            stream_id="s1", channel="mock", chat_id="u1", delta="", seq=2, is_end=True,
        ))

        await asyncio.wait_for(task, timeout=1)

        # _send_chunk 默认 fallback 到 _send_text
        assert len(ch.sent_texts) == 1
        assert ch.sent_texts[0] == ("u1", "chunk1")

    @pytest.mark.asyncio
    async def test_stream_error_notifies(self):
        """流式传输中 error → 发送中断提示。"""
        bus = MessageBus()
        ch = _MockChannel(bus)

        task = asyncio.create_task(ch.send_stream("u1"))

        await bus.publish_stream_delta(StreamDelta(
            stream_id="s1", channel="mock", chat_id="u1",
            error="connection lost",
        ))

        await asyncio.wait_for(task, timeout=1)

        assert len(ch.sent_texts) == 1
        assert "响应中断" in ch.sent_texts[0][1]


# ---------------------------------------------------------------------------
# _on_before_send 钩子
# ---------------------------------------------------------------------------

class _MediaChannel(_MockChannel):
    """覆写 _on_before_send 的 Mock 频道 — 记录钩子调用。"""

    def __init__(self, bus, **kw):
        super().__init__(bus, **kw)
        self.hook_calls: list = []  # 记录每次调用收到的 msg

    async def _on_before_send(self, msg):
        self.hook_calls.append(msg)
        # 处理媒体：模拟发送
        for ref in msg.media:
            self.sent_texts.append((msg.chat_id, f"[media: {ref}]"))


class TestBeforeSendHook:
    @pytest.mark.asyncio
    async def test_hook_not_called_for_confirmation(self):
        """confirmation_request 不触发 _on_before_send。"""
        bus = MessageBus()
        ch = _MediaChannel(bus)
        msg = OutboundMessage(
            channel="mock", chat_id="u1",
            msg_type="confirmation_request",
            request_id="req-1",
            content="confirm?",
            media=["/tmp/img.png"],
            timeout_seconds=5,
        )
        await ch.send(msg)
        # 确认拦截路径不走钩子
        assert len(ch.hook_calls) == 0
        # 媒体未被发送
        assert not any("media" in t for _, t in ch.sent_texts)

    @pytest.mark.asyncio
    async def test_hook_called_before_send_text(self):
        """_on_before_send 在 _send_text 之前调用。"""
        bus = MessageBus()
        ch = _MediaChannel(bus)
        msg = OutboundMessage(
            channel="mock", chat_id="u1",
            content="hello",
            media=["/tmp/img.png", "/tmp/file.pdf"],
        )
        await ch.send(msg)

        # 钩子被调用
        assert len(ch.hook_calls) == 1
        assert ch.hook_calls[0].media == ["/tmp/img.png", "/tmp/file.pdf"]

        # 媒体先发送（在 sent_texts 中排在前面）
        assert ("u1", "[media: /tmp/img.png]") in ch.sent_texts
        assert ("u1", "[media: /tmp/file.pdf]") in ch.sent_texts
        # 文本后发送
        assert ("u1", "hello") in ch.sent_texts

        # 顺序: media → media → text
        text_idx = ch.sent_texts.index(("u1", "hello"))
        media_idx_1 = ch.sent_texts.index(("u1", "[media: /tmp/img.png]"))
        assert media_idx_1 < text_idx

    @pytest.mark.asyncio
    async def test_no_media_still_calls_hook(self):
        """无 media 时钩子仍被调用（no-op）。"""
        bus = MessageBus()
        ch = _MediaChannel(bus)
        msg = OutboundMessage(channel="mock", chat_id="u1", content="hello")
        await ch.send(msg)
        assert len(ch.hook_calls) == 1
        assert ch.sent_texts == [("u1", "hello")]

    @pytest.mark.asyncio
    async def test_default_hook_is_noop(self):
        """默认 _on_before_send 是 no-op，不影响正常发送。"""
        bus = MessageBus()
        ch = _MockChannel(bus)  # 不覆写 _on_before_send
        msg = OutboundMessage(
            channel="mock", chat_id="u1",
            content="hello",
            media=["/tmp/img.png"],
        )
        await ch.send(msg)
        # 默认钩子是 no-op，只有 _send_text 被调用
        assert ch.sent_texts == [("u1", "hello")]


# ---------------------------------------------------------------------------
# OutboundMessage.media 字段
# ---------------------------------------------------------------------------

class TestOutboundMediaField:
    def test_media_defaults_to_empty(self):
        msg = OutboundMessage(channel="test", chat_id="u1", content="hi")
        assert msg.media == []

    def test_media_preserved(self):
        msg = OutboundMessage(
            channel="test", chat_id="u1", content="hi",
            media=["/tmp/a.png", "https://cdn.example.com/b.jpg"],
        )
        assert len(msg.media) == 2
        assert msg.media[0] == "/tmp/a.png"
        assert msg.media[1] == "https://cdn.example.com/b.jpg"


# ---------------------------------------------------------------------------
# S5: 确认 FIFO 精确匹配
# ---------------------------------------------------------------------------

class TestConfirmationFIFO:
    @pytest.mark.asyncio
    async def test_fifo_order_same_chat(self):
        """同 chat 两个 confirmations → FIFO 取最早的。"""
        bus = MessageBus()
        ch = _MockChannel(bus)
        ch._pending_confirmations["u1"] = ["req-1", "req-2"]
        ch._parse_return = {
            "chat_id": "u1", "content": "Y", "platform_msg_id": "m1",
        }

        # 第一次回复 → 匹配 req-1
        await ch.on_message({"raw": "msg1"})
        inbound1 = await bus.input_queue.get()
        assert inbound1.ref_request_id == "req-1"

        # 第二次回复 → 匹配 req-2
        ch._parse_return = {
            "chat_id": "u1", "content": "N", "platform_msg_id": "m2",
        }
        await ch.on_message({"raw": "msg2"})
        inbound2 = await bus.input_queue.get()
        assert inbound2.ref_request_id == "req-2"
        assert inbound2.content == "denied"

        # pending 列表已清空
        assert "u1" not in ch._pending_confirmations

    @pytest.mark.asyncio
    async def test_prompt_includes_request_id(self):
        """fallback prompt 包含 request_id 前 8 字符。"""
        bus = MessageBus()
        ch = _MockChannel(bus, supports_buttons=False)
        msg = OutboundMessage(
            channel="mock", chat_id="u1",
            msg_type="confirmation_request",
            request_id="abcdef1234567890",
            content="test",
            timeout_seconds=5,
        )
        await ch.send(msg)
        sent_text = ch.sent_texts[0][1]
        assert "abcdef12" in sent_text


# ---------------------------------------------------------------------------
# M6: TTL Task 生命周期
# ---------------------------------------------------------------------------

class TestTTLTaskLifecycle:
    @pytest.mark.asyncio
    async def test_ttl_task_cancelled_on_resolve(self):
        """用户回复确认后，对应 TTL task 被 cancel。"""
        bus = MessageBus()
        ch = _MockChannel(bus)

        # 通过 send() 创建 pending + TTL task
        msg = OutboundMessage(
            channel="mock", chat_id="u1",
            msg_type="confirmation_request",
            request_id="req-1",
            content="confirm?",
            timeout_seconds=60,
        )
        await ch.send(msg)
        assert "u1" in ch._ttl_tasks
        ttl_task = ch._ttl_tasks["u1"]
        assert not ttl_task.done()

        # 用户回复确认
        ch._parse_return = {
            "chat_id": "u1", "content": "Y", "platform_msg_id": "m1",
        }
        await ch.on_message({"raw": "msg"})
        await asyncio.sleep(0)  # 让 event loop 处理 cancel 传播

        # TTL task 已取消且从 dict 移除
        assert ttl_task.cancelled() or ttl_task.done()
        assert "u1" not in ch._ttl_tasks

    @pytest.mark.asyncio
    async def test_ttl_tasks_cleared_on_stop(self):
        """stop() 后所有 TTL task 被 cancel。"""
        bus = MessageBus()
        ch = _MockChannel(bus)

        # 创建两个 pending confirmations
        for i, uid in enumerate(["u1", "u2"]):
            msg = OutboundMessage(
                channel="mock", chat_id=uid,
                msg_type="confirmation_request",
                request_id=f"req-{i}",
                content="confirm?",
                timeout_seconds=60,
            )
            await ch.send(msg)

        assert len(ch._ttl_tasks) == 2

        await ch.stop()

        # 所有 TTL task 被清理
        assert len(ch._ttl_tasks) == 0
        assert len(ch._pending_confirmations) == 0


# ---------------------------------------------------------------------------
# M3: 异常继承体系
# ---------------------------------------------------------------------------

class TestChannelErrorHierarchy:
    def test_all_errors_inherit_from_channel_error(self):
        for cls in (ChannelTransientError, ChannelFatalError, ChannelAuthError):
            assert issubclass(cls, ChannelError)

    def test_channel_error_can_catch_all(self):
        for exc_cls in (ChannelTransientError, ChannelFatalError, ChannelAuthError):
            try:
                raise exc_cls("test")
            except ChannelError:
                pass  # expected

    def test_fatal_error_is_exception(self):
        assert issubclass(ChannelFatalError, Exception)
