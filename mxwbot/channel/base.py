"""BaseChannel — 模板方法模式的频道抽象基类。

子类只需实现 4 个抽象方法，流程逻辑（确认拦截、流式、TTL）由基类统一管理。

设计模式: Template Method — 基类拥有不变算法骨架，子类填充平台相关步骤。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

from mxwbot.bus.messages import InboundMessage, OutboundMessage, StreamDelta
from mxwbot.bus.queue import MessageBus

logger = logging.getLogger("mxwbot.channel.base")


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class ChannelError(Exception):
    """频道异常基类 — 所有频道相关异常的父类。"""


class ChannelTransientError(ChannelError):
    """可重试异常 — 网络波动、限流等短暂故障。"""


class ChannelFatalError(ChannelError):
    """不可恢复异常 — 配置错误、SDK 缺失等永久性故障。"""


class ChannelAuthError(ChannelError):
    """认证失败 — token 过期、凭据无效等认证类故障。"""


class BaseChannel(ABC):
    """频道抽象基类 — 模板方法模式。

    子类必须定义类属性 ``name`` 并实现 3 个抽象方法:
      - ``_start()``
      - ``_stop()``
      - ``_send_text(chat_id, text)``
      - ``supports_interactive_buttons() → bool``

    可选覆写:
      - ``_on_before_send(msg)`` — 发送文本前处理富媒体（默认 no-op）
      - ``_send_chunk(chat_id, chunk)`` — 默认 fallback 到 ``_send_text``
      - ``supports_streaming()`` — 默认 False
    """

    # -- 子类必须定义 --------------------------------------------------------

    name: str = ""

    # -- 构造 ----------------------------------------------------------------

    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus
        self._running = False
        # chat_id → [request_id, ...] FIFO，确认请求挂起表
        self._pending_confirmations: dict[str, list[str]] = {}
        # chat_id → asyncio.Task，TTL 清理任务
        self._ttl_tasks: dict[str, asyncio.Task] = {}

    # -- 子类必须实现的抽象方法 ----------------------------------------------

    @abstractmethod
    async def _start(self) -> None:
        """启动平台连接（登录、监听、轮询启动）。"""

    @abstractmethod
    async def _stop(self) -> None:
        """断开平台连接（登出、清理连接）。"""

    @abstractmethod
    async def _send_text(self, chat_id: str, text: str) -> None:
        """发送纯文本到指定 chat_id。"""

    @abstractmethod
    def supports_interactive_buttons(self) -> bool:
        """平台是否支持交互式按钮（如微信模板消息 / QQ 按钮）。"""

    # -- 子类可选覆写 --------------------------------------------------------

    def _parse(self, raw_msg: Any) -> dict | None:
        """默认实现：接受 dict 输入（测试/CLI 模拟模式）。

        平台自行消费消息的 Channel（QQ botpy 回调 / 微信长轮询 / Email IMAP）
        不需要覆写此方法。走 ``BaseChannel.on_message()`` 管道时自动调用。

        Returns:
            ``{"chat_id": str, "content": str, "platform_msg_id": str}``
            或 ``None``（无法解析时静默丢弃）。
        """
        if isinstance(raw_msg, dict):
            chat_id = raw_msg.get("chat_id", "")
            content = raw_msg.get("content", "")
            msg_id = raw_msg.get("message_id", "")
            if not chat_id or not content:
                return None
            return {
                "chat_id": str(chat_id),
                "content": str(content),
                "platform_msg_id": str(msg_id),
            }
        return None

    async def _on_before_send(self, msg: OutboundMessage) -> None:
        """发送文本前的钩子 — 子类覆写以处理富媒体/附件。

        在 ``_send_text`` 之前调用。子类在此钩子中遍历 ``msg.media``
        和 ``msg.metadata`` 发送图片、文件等。发送失败后可调用
        ``_send_text`` 发送错误通知。

        默认 no-op。
        """

    async def _send_chunk(self, chat_id: str, chunk: str) -> None:
        """发送流式增量文本。默认 fallback 到 ``_send_text``。"""
        await self._send_text(chat_id, chunk)

    def supports_streaming(self) -> bool:
        """平台是否支持真正的流式发送。默认 False。"""
        return False

    # -- 模板方法（子类不覆写）-----------------------------------------------

    async def start(self) -> None:
        """启动频道：连接平台 → 标记运行状态。"""
        self._running = True
        await self._start()

    async def stop(self) -> None:
        """停止频道：断开连接 → 清理挂起确认。"""
        self._running = False
        for task in self._ttl_tasks.values():
            if not task.done():
                task.cancel()
        self._ttl_tasks.clear()
        self._pending_confirmations.clear()
        await self._stop()

    async def send(self, msg: OutboundMessage) -> None:
        """发送消息到频道。

        ``confirmation_request`` 消息自动进入拦截模式：
        记录 pending → TTL 调度 → 发送确认提示。
        普通消息先调用 ``_on_before_send`` 钩子（子类处理富媒体），
        再调用 ``_send_text`` 发送文本。
        """
        if msg.msg_type == "confirmation_request":
            chat_id = msg.chat_id
            request_id = msg.request_id
            if not request_id:
                logger.warning("confirmation_request missing request_id, skip")
                return
            self._pending_confirmations.setdefault(chat_id, []).append(request_id)

            # Cancel existing TTL task for this chat before scheduling new one
            old_task = self._ttl_tasks.get(chat_id)
            if old_task is not None and not old_task.done():
                old_task.cancel()
            self._ttl_tasks[chat_id] = self._schedule_confirmation_ttl(
                chat_id, request_id, msg.timeout_seconds or 60,
            )

            if self.supports_interactive_buttons():
                await self._render_confirmation_buttons(msg)
            else:
                fallback = msg.fallback_prompt or (
                    f"即将执行写操作 (确认码: {request_id[:8]}), "
                    f"回复 Y 确认，N 拒绝"
                )
                await self._send_text(chat_id, fallback)
        else:
            await self._on_before_send(msg)
            await self._send_text(msg.chat_id, msg.content)

    async def on_message(self, raw_msg: Any) -> None:
        """接收平台消息入口。

        1. 解析原始消息 → 标准字段
        2. 检查是否在确认拦截模式 → 是则投递 confirmation_response
        3. 否则投递正常 InboundMessage 到 Bus
        """
        parsed = self._parse(raw_msg)
        if parsed is None:
            return  # 无法解析，静默丢弃

        chat_id = parsed.get("chat_id", "")
        content = parsed.get("content", "")

        # 确认拦截模式检测 — FIFO: 取最早的 pending request
        if chat_id and chat_id in self._pending_confirmations:
            pending_list = self._pending_confirmations[chat_id]
            request_id = pending_list.pop(0)
            if not pending_list:
                del self._pending_confirmations[chat_id]
            # Cancel the TTL task for this resolved request
            if chat_id in self._ttl_tasks:
                self._ttl_tasks[chat_id].cancel()
                del self._ttl_tasks[chat_id]
            approved = content.strip().upper() in (
                "Y", "YES", "是", "确认", "同意",
            )
            await self.bus.publish_inbound(InboundMessage(
                msg_type="confirmation_response",
                channel=self.name,
                chat_id=chat_id,
                content="approved" if approved else "denied",
                ref_request_id=request_id,
            ))
            return

        # 正常消息投递
        platform_msg_id = parsed.get("platform_msg_id", "")
        await self.bus.publish_inbound(InboundMessage(
            msg_type="message",
            channel=self.name,
            chat_id=chat_id,
            content=content,
            idempotency_key=(
                f"{self.name}:{chat_id}:{platform_msg_id}"
                if platform_msg_id else ""
            ),
        ))

    async def send_stream(self, chat_id: str) -> None:
        """流式输出 — 从 Bus 订阅 StreamDelta 队列并逐块发送。

        通道不支持流式时，整个流结束后一次性发送。
        """
        queue = await self.bus.subscribe_stream(self.name)

        if not self.supports_streaming():
            # 非流式模式：累积全部 delta，最后一次性发送
            buffer: list[str] = []
            while True:
                delta: StreamDelta = await queue.get()
                if delta.error:
                    await self._send_text(chat_id, "".join(buffer) + f"\n[响应中断: {delta.error}]")
                    return
                if delta.delta:
                    buffer.append(delta.delta)
                if delta.is_end:
                    await self._send_text(chat_id, "".join(buffer))
                    return
        else:
            # 流式模式：逐块发送
            while True:
                delta: StreamDelta = await queue.get()
                if delta.error:
                    await self._send_chunk(chat_id, f"\n[响应中断: {delta.error}]")
                    break
                if delta.delta:
                    await self._send_chunk(chat_id, delta.delta)
                if delta.is_end:
                    break

    # -- 内部辅助 ------------------------------------------------------------

    def _schedule_confirmation_ttl(
        self, chat_id: str, request_id: str, timeout_seconds: int,
    ) -> asyncio.Task:
        """创建后台任务，超时后自动清除过期确认挂起。

        防止用户在确认超时后回复 Y/N 被误拦截。
        返回创建的 asyncio.Task，调用方负责存储引用。
        """
        async def _cleanup() -> None:
            await asyncio.sleep(timeout_seconds)
            if chat_id in self._pending_confirmations:
                pending_list = self._pending_confirmations[chat_id]
                if request_id in pending_list:
                    pending_list.remove(request_id)
                if not pending_list:
                    del self._pending_confirmations[chat_id]
            self._ttl_tasks.pop(chat_id, None)

        return asyncio.create_task(_cleanup())

    async def _render_confirmation_buttons(self, msg: OutboundMessage) -> None:
        """渲染交互式确认按钮。

        有按钮能力的子类覆写此方法发送按钮 UI。
        默认 fallback 到纯文本。
        """
        fallback = msg.fallback_prompt or (
            f"即将执行写操作，回复 Y 确认，N 拒绝"
        )
        await self._send_text(msg.chat_id, fallback)

    # -- 健康检查 ------------------------------------------------------------

    def is_healthy(self) -> bool:
        """频道健康状态检查。

        返回 True 表示频道正常运行。子类可覆写加入平台相关检查。
        """
        return self._running

    def is_connected(self) -> bool:
        """平台连接状态检查。

        与 ``is_healthy()`` 不同，此方法检查底层平台连接是否活跃。
        子类应覆写此方法加入平台特定的连接检测。
        """
        return self._running
