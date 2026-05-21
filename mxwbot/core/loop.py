"""LoopPool + Loop — 中央编排器 (Phase 8).

LoopPool 是长驻消费者，从 MessageBus 持续拉取入站消息，
按 ``msg_type`` 分发：
  * ``confirmation_response`` → 唤醒挂起的确认请求
  * ``message`` → 创建 Loop 实例执行完整 turn

Loop 是每消息瞬态编排器，驱动 StateManager 走完 8 个状态：
  COMMAND → RESTORE → COMPACT → BUILD → RUN → SAVE → RESPOND → DONE

并发模型：
  * ``asyncio.Semaphore(20)`` 全局并发上限
  * ``asyncio.Lock`` 保证同一 session 消息串行处理
  * 不同 session 并行处理
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from mxwbot.bus.messages import InboundMessage, OutboundMessage, StreamDelta
from mxwbot.bus.queue import MessageBus
from mxwbot.checkpoint.manager import CheckpointManager, CheckpointSnapshot
from mxwbot.core.context import ContextBuilder
from mxwbot.core.hook import AgentHook
from mxwbot.core.runner import AgentRunner, AgentRunSpec, AgentRunResult
from mxwbot.core.skill import SkillLoader
from mxwbot.core.state import (
    DegradationAction,
    DegradationPolicy,
    StateManager,
    TurnState,
)
from mxwbot.memory.core import MemoryManager
from mxwbot.memory.token_budget import count_tokens
from mxwbot.providers.base import LLMCallPurpose, LLMProvider
from mxwbot.session.manager import Session, SessionManager

logger = logging.getLogger("mxwbot.core.loop")

# Token budget for triggering COMPACT
_COMPACT_USAGE_RATIO = 0.8
_COMPACT_MSG_COUNT = 20


# ---------------------------------------------------------------------------
# LoopContext
# ---------------------------------------------------------------------------


@dataclass
class LoopContext:
    """Per-turn context passed through state handlers.

    Each Loop instance owns one LoopContext.  State handlers read from
    and write to this context without caring about which state comes
    before or after.
    """

    msg: InboundMessage
    session: Session
    session_key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    result: AgentRunResult | None = None
    summary: str = ""
    checkpoint_restored: bool = False
    stop_requested: bool = False
    msg_count_before_run: int = 0


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------


class Loop:
    """Per-turn orchestrator.

    Created fresh for each inbound message and discarded after the turn
    completes.  Drives an internal ``StateManager`` through the standard
    8-state pipeline.  Handlers are async methods that return event
    strings ("ok" / "error" / "shortcut" / "dispatch").
    """

    def __init__(
        self,
        ctx: LoopContext,
        *,
        sessions: SessionManager,
        memory: MemoryManager,
        context_builder: ContextBuilder,
        runner: AgentRunner,
        provider: LLMProvider,
        tools: Any,  # ToolRegistry
        checkpoint: CheckpointManager,
        skills: SkillLoader,
        bus: MessageBus,
        checkpoint_interval: int = 2,
        max_iterations: int = 3,
        max_context_tokens: int = 80_000,
    ) -> None:
        self.ctx = ctx
        self._sessions = sessions
        self._memory = memory
        self._context_builder = context_builder
        self._runner = runner
        self._provider = provider
        self._tools = tools
        self._checkpoint = checkpoint
        self._skills = skills
        self._bus = bus
        self._checkpoint_interval = checkpoint_interval
        self._max_iterations = max_iterations
        self._max_context_tokens = max_context_tokens

        # -- handler table ---------------------------------------------------
        self._handlers: dict[TurnState, Callable[[], Any]] = {
            TurnState.COMMAND: self._handle_command,
            TurnState.RESTORE: self._handle_restore,
            TurnState.COMPACT: self._handle_compact,
            TurnState.BUILD: self._handle_build,
            TurnState.RUN: self._handle_run,
            TurnState.SAVE: self._handle_save,
            TurnState.RESPOND: self._handle_respond,
        }

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Execute one full turn through the state machine."""
        sm = StateManager()
        try:
            while not sm.is_terminal:
                handler = self._handlers.get(sm.current)
                if handler is None:
                    break  # DONE or unknown
                event = await handler()
                sm.dispatch(event)

            # Turn completed normally — cleanup
            await self._finish()

        except Exception as exc:
            logger.error("Loop crashed for %s: %s", self.ctx.session_key, exc)

            # Emergency checkpoint
            decision = DegradationPolicy.handle("runner_exception", exc)
            if decision.action in (DegradationAction.RETRY,):
                await self._emergency_checkpoint()
            # Try to respond with error
            await self._send_error_reply(str(exc))

    # ------------------------------------------------------------------
    # COMMAND: detect slash commands
    # ------------------------------------------------------------------

    async def _handle_command(self) -> str:
        """Detect slash commands.  Shortcut past RESTORE→...→RUN if handled."""
        content = (self.ctx.msg.content or "").strip()
        if not content.startswith("/"):
            return "dispatch"  # Normal message flow

        cmd = content.lower().split()[0]
        if cmd == "/clear":
            self.ctx.session.clear()
            self.ctx.result = AgentRunResult(
                content="对话已清空。",
                finish_reason="stop",
            )
            return "shortcut"
        elif cmd == "/stop":
            self.ctx.result = AgentRunResult(
                content="Bot 已停止当前会话。",
                finish_reason="stop",
            )
            self.ctx.stop_requested = True
            return "shortcut"
        elif cmd == "/status":
            msgs = self.ctx.session.message_count
            consolidated = self.ctx.session.consolidated_count
            self.ctx.result = AgentRunResult(
                content=f"会话状态: {msgs} 条消息, {consolidated} 条已压缩。",
                finish_reason="stop",
            )
            return "shortcut"
        else:
            # Unknown command — treat as normal message
            return "dispatch"

    # ------------------------------------------------------------------
    # RESTORE: load pending checkpoint
    # ------------------------------------------------------------------

    async def _handle_restore(self) -> str:
        """Check for an unfinished checkpoint and restore it."""
        snapshot = await self._checkpoint.load_latest(self.ctx.session_key)
        if snapshot is None:
            return "ok"

        logger.info(
            "Loop: restoring checkpoint %s for %s (iter=%d)",
            snapshot.id, self.ctx.session_key, snapshot.iteration,
        )
        self.ctx.messages = snapshot.messages
        self.ctx.checkpoint_restored = True
        # Rebuild session summary from restored messages if needed
        return "ok"

    # ------------------------------------------------------------------
    # COMPACT: compress history if token budget is tight
    # ------------------------------------------------------------------

    async def _handle_compact(self) -> str:
        """Conditionally compact old messages into a summary."""
        session = self.ctx.session
        unconsolidated = session.messages[session.consolidated_count:]
        msg_count = len(unconsolidated)
        if msg_count <= _COMPACT_MSG_COUNT:
            return "ok"

        # Estimate usage
        try:
            tokens = count_tokens(unconsolidated)
        except Exception:
            tokens = sum(len(str(m.get("content", ""))) // 4 for m in unconsolidated)
        usage_ratio = tokens / self._max_context_tokens if self._max_context_tokens else 0

        if usage_ratio < _COMPACT_USAGE_RATIO:
            return "ok"

        # Trigger compact via MemoryManager (delegates to summarizer)
        logger.info(
            "Loop: compacting %s — %d tokens (%.0f%%)",
            self.ctx.session_key, tokens, usage_ratio * 100,
        )
        new_count, summary = await self._memory.consolidate(
            messages=session.messages,
            consolidated_count=session.consolidated_count,
            usage_ratio=usage_ratio,
            msg_count=session.message_count,
        )
        session.consolidated_count = new_count
        if summary:
            session.session_summary = (
                (session.session_summary or "") + "\n" + summary
            ).strip()
        self.ctx.summary = summary
        return "ok"

    # ------------------------------------------------------------------
    # BUILD: assemble LLM context
    # ------------------------------------------------------------------

    async def _handle_build(self) -> str:
        """Build the message list for the LLM call."""
        ctx = self.ctx
        self.ctx.messages = await self._context_builder.build(
            purpose=LLMCallPurpose.AGENT,
            session=ctx.session,
            user_msg=ctx.msg.content,
            memory_manager=self._memory,
            skill_loader=self._skills,
        )
        return "ok"

    # ------------------------------------------------------------------
    # RUN: execute ReAct loop
    # ------------------------------------------------------------------

    async def _handle_run(self) -> str:
        """Run the AgentRunner ReAct loop with streaming."""
        # Record message count so SAVE knows which messages Runner added
        self.ctx.msg_count_before_run = len(self.ctx.messages)

        # -- stream hook: publish text deltas to Bus in real time -----------
        channel = self.ctx.msg.channel
        chat_id = self.ctx.msg.chat_id

        class _BusStreamHook(AgentHook):
            """Forwards text deltas to Bus stream queue in real time."""
            def __init__(self, bus: Any, channel: str, chat_id: str) -> None:
                self._bus = bus
                self._channel = channel
                self._chat_id = chat_id
                self._seq = 0

            async def on_stream_delta(self, delta: str) -> None:
                self._seq += 1
                await self._bus.publish_stream_delta(StreamDelta(
                    stream_id=f"{self._channel}:{self._chat_id}",
                    channel=self._channel, chat_id=self._chat_id,
                    delta=delta, seq=self._seq,
                ))

        stream_hook = _BusStreamHook(self._bus, channel, chat_id)

        # Signal stream start
        await self._bus.publish_stream_delta(StreamDelta(
            stream_id=f"{channel}:{chat_id}",
            channel=channel, chat_id=chat_id,
            delta="", seq=0,
        ))

        async def _on_checkpoint(msgs: list, iteration: int) -> None:
            """Capture a snapshot before tool execution."""
            await self._checkpoint.save(CheckpointSnapshot(
                session_id=self.ctx.session_key,
                iteration=iteration,
                messages=list(msgs),
                state="RUN",
            ))

        spec = AgentRunSpec(
            provider=self._provider,
            tools=self._tools,
            max_iterations=self._max_iterations,
            checkpoint_interval=self._checkpoint_interval,
            bus=self._bus,
            hook=stream_hook,
            on_checkpoint=_on_checkpoint,
        )
        self.ctx.result = await self._runner.run_stream(spec, self.ctx.messages)

        if self.ctx.result.finish_reason == "error":
            return "error"
        return "ok"

    # ------------------------------------------------------------------
    # SAVE: persist inbound message to session
    # ------------------------------------------------------------------

    async def _handle_save(self) -> str:
        """Save the inbound message to the session JSONL."""
        # Record the user message
        self._sessions.mark_seen(self.ctx.msg)
        await self._sessions.save_inbound(self.ctx.session, self.ctx.msg)

        # Persist intermediate assistant/tool messages added by Runner
        for msg in self.ctx.messages[self.ctx.msg_count_before_run:]:
            role = msg.get("role", "")
            if role in ("assistant", "tool"):
                await self.ctx.session.append_message(msg)

        # Append final assistant response if not already included above
        result = self.ctx.result
        if result and result.content:
            already_saved = any(
                m.get("role") == "assistant" and m.get("content") == result.content
                for m in self.ctx.messages[self.ctx.msg_count_before_run:]
            )
            if not already_saved:
                await self.ctx.session.append_message({
                    "role": "assistant",
                    "content": result.content,
                })

        return "ok"

    # ------------------------------------------------------------------
    # RESPOND: send reply to channel
    # ------------------------------------------------------------------

    async def _handle_respond(self) -> str:
        """Deliver the final reply to the channel via MessageBus.

        Closes the stream with a terminal ``StreamDelta(is_end=True)``
        then publishes the full ``OutboundMessage`` as a fallback for
        channels that don't support streaming.
        """
        result = self.ctx.result
        if result is None:
            return "ok"

        channel = self.ctx.msg.channel
        chat_id = self.ctx.msg.chat_id
        content = result.content or ""

        # Close the stream — channel.send_stream() uses this as terminal
        await self._bus.publish_stream_delta(StreamDelta(
            stream_id=f"{channel}:{chat_id}",
            channel=channel, chat_id=chat_id,
            delta="", seq=-1, is_end=True,
        ))

        # Fallback: full message for non-streaming consumers
        await self._bus.publish_outbound(OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            msg_type="message",
            reply_to=self.ctx.msg.id,
        ))
        return "ok"

    # ------------------------------------------------------------------
    # Post-turn cleanup
    # ------------------------------------------------------------------

    async def _finish(self) -> None:
        """Cleanup after a successful turn."""
        # Honour /stop request — close session after responding
        if self.ctx.stop_requested:
            try:
                await self._sessions.close(self.ctx.session)
            except Exception:
                pass

        # Prune old checkpoints
        try:
            await self._checkpoint.prune(self.ctx.session_key)
        except Exception:
            pass

        # Extract long-term facts (compact was already handled in
        # _handle_compact; pass usage_ratio=0 to skip re-compacting).
        try:
            session = self.ctx.session
            new_count, _ = await self._memory.consolidate(
                messages=session.messages,
                consolidated_count=session.consolidated_count,
                usage_ratio=0.0,
                msg_count=0,
            )
            session.consolidated_count = new_count
        except Exception:
            logger.warning(
                "Memory consolidation failed for %s", self.ctx.session_key,
            )

    # ------------------------------------------------------------------
    # Error recovery
    # ------------------------------------------------------------------

    async def _emergency_checkpoint(self) -> None:
        """Save an emergency checkpoint before crashing."""
        try:
            await self._checkpoint.save(CheckpointSnapshot(
                session_id=self.ctx.session_key,
                iteration=0,
                messages=list(self.ctx.messages),
                state="RUN",
            ))
        except Exception:
            logger.error(
                "Failed to save emergency checkpoint for %s",
                self.ctx.session_key,
            )

    async def _send_error_reply(self, error_msg: str) -> None:
        """Send a brief error message to the user."""
        try:
            await self._bus.publish_outbound(OutboundMessage(
                channel=self.ctx.msg.channel,
                chat_id=self.ctx.msg.chat_id,
                content=f"[系统错误] 处理请求时遇到问题。{error_msg[:200]}",
                msg_type="message",
                reply_to=self.ctx.msg.id,
            ))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# LoopPool — concurrent message consumer
# ---------------------------------------------------------------------------

class LoopPool:
    """Long-lived message consumer with concurrency control.

    Reads ``InboundMessage`` from ``MessageBus.input_queue`` and
    dispatches each to a new ``Loop`` task.  Different sessions run in
    parallel (subject to ``Semaphore(20)`` global limit); messages
    within the same session are serialised via ``asyncio.Lock``.

    Confirmation responses are routed directly to the pending
    ``MessageBus.request_confirmation()`` event.
    """

    def __init__(
        self,
        bus: MessageBus,
        sessions: SessionManager,
        memory: MemoryManager,
        context_builder: ContextBuilder,
        runner: AgentRunner,
        provider: LLMProvider,
        tools: Any,  # ToolRegistry
        checkpoint: CheckpointManager,
        skills: SkillLoader,
        *,
        max_concurrent: int = 20,
        checkpoint_interval: int = 2,
        max_iterations: int = 3,
        max_context_tokens: int = 80_000,
    ) -> None:
        self._bus = bus
        self._sessions = sessions
        self._memory = memory
        self._context_builder = context_builder
        self._runner = runner
        self._provider = provider
        self._tools = tools
        self._checkpoint = checkpoint
        self._skills = skills
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._checkpoint_interval = checkpoint_interval
        self._max_iterations = max_iterations
        self._max_context_tokens = max_context_tokens
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Begin consuming messages from the Bus indefinitely."""
        self._running = True
        logger.info("LoopPool started (max_concurrent=%d)", self._semaphore._value)
        try:
            await self._consume()
        except asyncio.CancelledError:
            logger.info("LoopPool consumer cancelled")
        finally:
            self._running = False

    async def stop(self) -> None:
        """Gracefully stop the consumer loop."""
        self._running = False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _consume(self) -> None:
        """Main consumer loop — reads from bus.input_queue with 1s poll.

        Uses ``asyncio.wait_for`` with a 1 s timeout so that
        ``stop()`` (which sets ``_running = False``) takes effect
        within one second even when the queue is idle.
        """
        while self._running:
            try:
                msg = await asyncio.wait_for(
                    self._bus.input_queue.get(), timeout=1.0,
                )
            except asyncio.TimeoutError:
                continue  # Re-check self._running
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.1)
                continue

            if msg.msg_type == "confirmation_response":
                self._resolve_confirmation(msg)
            else:
                asyncio.create_task(self._dispatch(msg))

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a normal (non-confirmation) inbound message.

        Enforces:
          * Duplicate detection
          * Global concurrency limit (Semaphore)
          * Per-session serialisation (asyncio.Lock)
        """
        # 1. Duplicate detection
        if self._sessions.is_duplicate(msg):
            logger.debug("LoopPool: dropping duplicate %s", msg.idempotency_key)
            return

        # 2. Acquire session
        session = await self._sessions.get_session(msg.channel, msg.chat_id)
        session_key = f"{msg.channel}:{msg.chat_id}"

        # 3. Concurrency control
        async with self._semaphore:
            async with self._sessions.get_lock(msg.channel, msg.chat_id):
                ctx = LoopContext(
                    msg=msg,
                    session=session,
                    session_key=session_key,
                )
                loop = Loop(
                    ctx,
                    sessions=self._sessions,
                    memory=self._memory,
                    context_builder=self._context_builder,
                    runner=self._runner,
                    provider=self._provider,
                    tools=self._tools,
                    checkpoint=self._checkpoint,
                    skills=self._skills,
                    bus=self._bus,
                    checkpoint_interval=self._checkpoint_interval,
                    max_iterations=self._max_iterations,
                    max_context_tokens=self._max_context_tokens,
                )
                await loop.run()

    def _resolve_confirmation(self, msg: InboundMessage) -> None:
        """Wake up a pending ``MessageBus.request_confirmation()`` call."""
        if not msg.ref_request_id:
            return
        approved = msg.content == "approved"
        self._bus.resolve_confirmation(msg.ref_request_id, approved)
