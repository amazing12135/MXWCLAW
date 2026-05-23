"""MessageBus — pure channel layer.

The Bus does **not** do any message dispatch or routing logic.
It just provides queues and a confirmation handshake.  The consumption
loop lives in ``LoopPool`` (Phase 8).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from mxwbot.bus.messages import InboundMessage, OutboundMessage, StreamDelta


class MessageBus:
    """Bidirectional message transport between Channels and the Core loop.

    All ``publish_*`` / ``subscribe_*`` methods are lock-free.  The only
    stateful section is the confirmation handshake, which uses
    ``asyncio.Event`` for blocking wait.
    """

    def __init__(self) -> None:
        # Channel → Core
        self.input_queue: asyncio.Queue[InboundMessage] = asyncio.Queue()

        # Core → Channel (one queue per channel name)
        self._output_queues: dict[str, asyncio.Queue[OutboundMessage]] = {}

        # Stream channels
        self._stream_queues: dict[str, asyncio.Queue[StreamDelta]] = {}

        # Confirmation handshake
        self._pending: dict[str, asyncio.Event] = {}
        self._results: dict[str, bool] = {}
        # Direct call responses: msg_id → (Future, on_stream_callback)
        self._pending_directs: dict[str, tuple[asyncio.Future, Any]] = {}

    # -- direct call ---------------------------------------------------------

    def register_direct(self, msg_id: str, future: asyncio.Future, on_stream: Any = None) -> None:
        self._pending_directs[msg_id] = (future, on_stream)

    def resolve_direct(self, msg_id: str, result: Any) -> bool:
        entry = self._pending_directs.pop(msg_id, None)
        if entry is None:
            return False
        fut, _ = entry
        if not fut.done():
            fut.set_result(result)
        return True

    @property
    def pending_directs(self) -> dict:
        return self._pending_directs

    # -- inbound ------------------------------------------------------------

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Push a message from a Channel into the input queue."""
        await self.input_queue.put(msg)

    # -- outbound -----------------------------------------------------------

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Push a message to the queue for *msg.channel*."""
        q = self._ensure_output_queue(msg.channel)
        await q.put(msg)

    async def subscribe(self, channel_name: str) -> "asyncio.Queue[OutboundMessage]":
        """Get (or create) the output queue for a channel."""
        return self._ensure_output_queue(channel_name)

    def _ensure_output_queue(self, name: str) -> "asyncio.Queue[OutboundMessage]":
        if name not in self._output_queues:
            self._output_queues[name] = asyncio.Queue()
        return self._output_queues[name]

    # -- streaming ----------------------------------------------------------

    async def publish_stream_start(self, session_id: str) -> str:
        """Register a new stream and return its id."""
        stream_id = uuid.uuid4().hex[:12]
        # Use a per-channel key as well so Channels can subscribe
        return stream_id

    async def publish_stream_delta(self, delta: StreamDelta) -> None:
        """Push a stream chunk to its channel queue."""
        q = self._ensure_stream_queue(delta.channel)
        await q.put(delta)

    async def publish_stream_end(self, stream_id: str) -> None:
        """Mark a stream as finished (terminal chunk)."""
        # No-op — the terminal chunk is sent via publish_stream_delta
        # with is_end=True or error set.
        pass

    async def subscribe_stream(self, channel_name: str) -> "asyncio.Queue[StreamDelta]":
        """Get (or create) the stream queue for a channel."""
        return self._ensure_stream_queue(channel_name)

    def _ensure_stream_queue(self, name: str) -> "asyncio.Queue[StreamDelta]":
        if name not in self._stream_queues:
            self._stream_queues[name] = asyncio.Queue()
        return self._stream_queues[name]

    # -- confirmation -------------------------------------------------------

    async def request_confirmation(self, req: OutboundMessage) -> bool:
        """Send a confirmation request and block until the user responds
        (or the timeout expires).

        Returns True if the user approved, False otherwise.
        """
        event = asyncio.Event()
        self._pending[req.request_id] = event
        await self.publish_outbound(req)

        try:
            await asyncio.wait_for(event.wait(), timeout=req.timeout_seconds or 60)
        except asyncio.TimeoutError:
            self._pending.pop(req.request_id, None)
            return False

        return self._results.pop(req.request_id, False)

    def resolve_confirmation(self, request_id: str, approved: bool) -> bool:
        """Called by the consumption loop when a ``confirmation_response``
        arrives.  Returns True if the event was found and resolved.
        """
        event = self._pending.pop(request_id, None)
        if event is None:
            return False
        self._results[request_id] = approved
        event.set()
        return True

    # -- inspection ---------------------------------------------------------

    @property
    def queue_depth_input(self) -> int:
        return self.input_queue.qsize()

    def queue_depth_output(self, channel_name: str) -> int:
        q = self._output_queues.get(channel_name)
        return q.qsize() if q else 0

    @property
    def pending_confirmations(self) -> int:
        return len(self._pending)
