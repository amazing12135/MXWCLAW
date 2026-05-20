"""Tests for bus/queue.py — MessageBus."""

import asyncio

import pytest

from mxwbot.bus.messages import InboundMessage, OutboundMessage, StreamDelta
from mxwbot.bus.queue import MessageBus


class TestMessageBus:
    @pytest.mark.asyncio
    async def test_publish_inbound_and_consume(self):
        bus = MessageBus()
        msg = InboundMessage(channel="wechat", chat_id="u1", content="hi")
        await bus.publish_inbound(msg)

        received = await bus.input_queue.get()
        assert received.channel == "wechat"
        assert received.content == "hi"

    @pytest.mark.asyncio
    async def test_publish_outbound_and_subscribe(self):
        bus = MessageBus()
        msg = OutboundMessage(channel="wechat", chat_id="u1", content="hello")

        # Subscribe first, then publish
        q = await bus.subscribe("wechat")
        await bus.publish_outbound(msg)

        received = await q.get()
        assert received.content == "hello"

    @pytest.mark.asyncio
    async def test_stream_delta_flow(self):
        bus = MessageBus()
        stream_q = await bus.subscribe_stream("wechat")

        await bus.publish_stream_delta(StreamDelta(
            stream_id="s1", channel="wechat", chat_id="u1",
            delta="Hello", seq=1,
        ))
        chunk = await stream_q.get()
        assert chunk.delta == "Hello"

    @pytest.mark.asyncio
    async def test_stream_error(self):
        bus = MessageBus()
        stream_q = await bus.subscribe_stream("wechat")

        await bus.publish_stream_delta(StreamDelta(
            stream_id="s1", channel="wechat", chat_id="u1",
            error="Connection lost", is_end=True,
        ))
        chunk = await stream_q.get()
        assert chunk.error == "Connection lost"

    @pytest.mark.asyncio
    async def test_confirmation_approved(self):
        bus = MessageBus()
        req = OutboundMessage(
            channel="wechat", chat_id="u1", content="Execute?",
            msg_type="confirmation_request",
            request_id="req-1", timeout_seconds=5,
        )

        async def simulate_user_approves():
            await asyncio.sleep(0.01)
            bus.resolve_confirmation("req-1", approved=True)

        asyncio.create_task(simulate_user_approves())
        result = await bus.request_confirmation(req)
        assert result is True

    @pytest.mark.asyncio
    async def test_confirmation_timeout(self):
        bus = MessageBus()
        req = OutboundMessage(
            channel="wechat", chat_id="u1", content="Execute?",
            msg_type="confirmation_request",
            request_id="req-2", timeout_seconds=0.01,
        )
        result = await bus.request_confirmation(req)
        assert result is False  # timeout = denied

    @pytest.mark.asyncio
    async def test_confirmation_denied(self):
        bus = MessageBus()
        req = OutboundMessage(
            channel="wechat", chat_id="u1", content="Execute?",
            msg_type="confirmation_request",
            request_id="req-3", timeout_seconds=5,
        )

        async def simulate_user_denies():
            await asyncio.sleep(0.01)
            bus.resolve_confirmation("req-3", approved=False)

        asyncio.create_task(simulate_user_denies())
        result = await bus.request_confirmation(req)
        assert result is False

    def test_queue_depth(self):
        bus = MessageBus()
        assert bus.queue_depth_input == 0
        assert bus.pending_confirmations == 0
