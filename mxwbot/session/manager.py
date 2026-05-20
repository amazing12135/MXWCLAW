"""Session manager — JSONL persistence + dedup + concurrency safety.

Design highlights:
  * JSONL append-only writes (row-level atomic).
  * ``asyncio.Lock`` per session prevents concurrent writes to the same file.
  * LRU dedup cache (100 keys) uses ``idempotency_key``.
  * Compact rewrites via tmp-file + ``os.replace()`` for crash safety.
  * Filename sanitisation prevents path traversal.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles

from mxwbot.bus.messages import InboundMessage
from mxwbot.config.path import get_session_path
from mxwbot.memory.token_budget import (
    _find_user_turn_start,
    _remove_orphaned_tool_results,
    count_tokens,
)
from mxwbot.utils.text import clean_assistant_replay_text


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """内存中的会话表示。"""

    session_key: str                # f"{channel}:{chat_id}"
    channel: str
    chat_id: str
    path: Path
    messages: list[dict[str, Any]] = field(default_factory=list)
    active_task: str | None = None  # Agent 当前正在执行的任务（Loop/Runner 设置）
    consolidated_count: int = 0     # 已被 compress 覆盖的消息数
    session_summary: str | None = None  # messages[:consolidated_count] 的压缩摘要
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def message_count(self) -> int:
        return len(self.messages)

    def clear(self) -> None:
        """清空会话运行时状态。

        JSONL 文件保留在磁盘上。下次 get_session 时重新加载。
        """
        self.messages.clear()
        self.active_task = None
        self.consolidated_count = 0
        self.session_summary = None

    async def append_message(self, msg: dict[str, Any]) -> None:
        """追加一条消息（user / assistant / tool / system）并写入 JSONL。

        先写磁盘，成功后再更新内存。

        Args:
            msg: 已格式化的消息字典，含 role / content 等字段。
        """
        async with aiofiles.open(str(self.path), "a", encoding="utf-8") as f:
            await f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.messages.append(msg)
        self.updated_at = datetime.now(timezone.utc)

    def get_recent(self, n: int = 10) -> list[dict[str, Any]]:
        """获取最近 N 轮对话消息。

        Args:
            n: 返回的消息条数。

        Returns:
            最近 N 条消息（按时间顺序）。
        """
        return self.messages[-n:] if self.messages else []

    def get_history(
        self,
        max_messages: int = 120,
        max_tokens: int = 0,
        include_timestamps: bool = False,
    ) -> list[dict[str, Any]]:
        """获取未压缩的对话历史，用于送入 LLM 上下文。

        处理管道：
            unconsolidated 切片 → user turn 对齐 → 去孤儿 tool result
            → assistant 文本清洗 → token 裁剪 → 再次对齐 → 再次去孤儿

        Args:
            max_messages: 消息数上限。
            max_tokens: token 数上限，0 表示不限制。
            include_timestamps: 是否在 user 消息前附加时间戳。

        Returns:
            LLM 可直接使用的消息列表。
        """
        # 只取未压缩部分
        raw = self.messages[self.consolidated_count:]
        if max_messages > 0:
            raw = raw[-max_messages:]

        # 1. 对齐到 user turn 起点（保留可能的 proactive assistant 消息）
        start = _find_user_turn_start(raw)
        if start > 0:
            raw = raw[start:]

        # 2. 去掉前导孤儿 tool result
        raw = _remove_orphaned_tool_results(raw)

        # 3. 构建输出：清洗 assistant 文本 + 合成 media 占位符
        out: list[dict[str, Any]] = []
        for msg in raw:
            content = msg.get("content", "")
            role = msg.get("role")

            if role == "assistant" and isinstance(content, str):
                content = clean_assistant_replay_text(content)

            # 图片占位符：media 字段 → [image: path] 文本
            media = msg.get("media")
            if role == "user" and isinstance(media, list) and media and isinstance(content, str):
                breadcrumbs = "\n".join(
                    f"[image: {p}]" for p in media if isinstance(p, str) and p
                )
                content = f"{content}\n{breadcrumbs}" if content else breadcrumbs

            if include_timestamps:
                ts = msg.get("timestamp", "")
                if ts and role == "user" and isinstance(content, str):
                    content = f"[Message Time: {ts}]\n{content}"

            entry: dict[str, Any] = {"role": msg["role"], "content": content}
            for key in ("tool_calls", "tool_call_id", "name", "reasoning_content", "thinking_blocks"):
                if key in msg:
                    entry[key] = msg[key]
            out.append(entry)

        # 4. Token 裁剪（从尾部倒序保留）
        if max_tokens > 0 and out:
            kept: list[dict[str, Any]] = []
            used = 0
            for msg in reversed(out):
                t = count_tokens([msg])
                if kept and used + t > max_tokens:
                    break
                kept.append(msg)
                used += t
            kept.reverse()
            out = kept

            # 5. Token 裁剪后再对齐
            start = _find_user_turn_start(out)
            if start > 0:
                out = out[start:]

            # 6. 再次去孤儿 tool
            out = _remove_orphaned_tool_results(out)

        return out


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

class SessionManager:
    """Persist sessions as JSONL files; provide dedup and per-session locks."""

    _DEDUP_WINDOW: int = 100
    _MAX_MESSAGES: int = 1000
    _COMPACT_KEEP: int = 200

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # LRU dedup: session_key → OrderedDict of idempotency_key → None
        self._dedup: dict[str, OrderedDict[str, None]] = {}

    # -- session retrieval --------------------------------------------------

    async def get_session(self, channel: str, chat_id: str) -> Session:
        """Return the in-memory Session, loading from disk if needed."""
        key = f"{channel}:{chat_id}"
        if key not in self._sessions:
            path = get_session_path(self._workspace, channel, chat_id)
            messages = await self._load_jsonl(path)
            session = Session(
                session_key=key, channel=channel, chat_id=chat_id,
                path=path, messages=messages,
            )
            self._sessions[key] = session
        return self._sessions[key]

    # -- dedup --------------------------------------------------------------

    def is_duplicate(self, msg: InboundMessage) -> bool:
        """Return True if *msg* has already been seen in this session."""
        session_key = f"{msg.channel}:{msg.chat_id}"
        dedup_set = self._dedup.get(session_key)
        if dedup_set is None:
            return False
        return msg.idempotency_key in dedup_set

    def mark_seen(self, msg: InboundMessage) -> None:
        """Record *msg* as seen, performing LRU eviction if needed."""
        session_key = f"{msg.channel}:{msg.chat_id}"
        if session_key not in self._dedup:
            self._dedup[session_key] = OrderedDict()
        dedup_set = self._dedup[session_key]
        dedup_set[msg.idempotency_key] = None
        # Evict oldest half when window fills
        if len(dedup_set) > self._DEDUP_WINDOW:
            overflow = len(dedup_set) - self._DEDUP_WINDOW // 2
            for _ in range(overflow):
                dedup_set.popitem(last=False)

    # -- persistence --------------------------------------------------------

    async def save_inbound(self, session: Session, msg: InboundMessage) -> None:
        """将入站消息转换为标准格式后追加到会话。

        Args:
            session: 目标会话。
            msg: 入站消息（来自 Channel）。
        """
        entry = {
            "role": "user",
            "content": msg.content,
            "timestamp": msg.timestamp.isoformat(),
            "msg_id": msg.id,
        }
        await session.append_message(entry)

        # Auto-compact if too large
        if session.message_count > self._MAX_MESSAGES:
            await self.compact(session)

    async def compact(self, session: Session, keep_last: int | None = None) -> None:
        """原子重写 JSONL 文件，保留结构性完整的尾部消息。

        截断前做 user turn 对齐 + 去孤儿 tool result，
        避免形成不完整的对话上下文。
        """
        keep = keep_last or self._COMPACT_KEEP
        if len(session.messages) <= keep:
            return

        # 取尾部并对齐
        retained = list(session.messages[-keep:])
        start = _find_user_turn_start(retained)
        if start > 0:
            retained = retained[start:]
        retained = _remove_orphaned_tool_results(retained)

        dropped = len(session.messages) - len(retained)

        path = get_session_path(self._workspace, session.channel, session.chat_id)
        tmp_path = path.with_suffix(".jsonl.tmp")
        try:
            async with aiofiles.open(str(tmp_path), "w", encoding="utf-8") as f:
                for msg in retained:
                    await f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            os.replace(tmp_path, path)
            session.messages = retained
            session.consolidated_count = max(0, session.consolidated_count - dropped)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    # -- locks --------------------------------------------------------------

    def get_lock(self, channel: str, chat_id: str) -> asyncio.Lock:
        """Return (and cache) an ``asyncio.Lock`` for this session.

        The lock ensures that messages for the same conversation are
        processed serially even though different conversations run in
        parallel.
        """
        key = f"{channel}:{chat_id}"
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    # -- session lifecycle --------------------------------------------------

    async def close(self, session: Session) -> None:
        """关闭会话：清空运行时状态并从缓存移除。

        Session.clear() 负责清空自身状态，
        SessionManager 负责从缓存/锁/去重表中移除。

        JSONL 文件保留在磁盘上，下次访问时自动重新加载。

        调用时机：
          - 用户主动退出
          - Heartbeat 检测到长时间不活动（待实现）

        Args:
            session: 要关闭的会话。
        """
        session.clear()
        self._sessions.pop(session.session_key, None)
        self._locks.pop(session.session_key, None)
        self._dedup.pop(session.session_key, None)

    # -- internal -----------------------------------------------------------

    async def _load_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            return await self._parse_jsonl(path)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            # File corrupted — repair by recovering valid lines
            return await self._repair_jsonl(path)

    async def _parse_jsonl(self, path: Path) -> list[dict[str, Any]]:
        """Full file parse — fast path for clean files."""
        messages: list[dict[str, Any]] = []
        async with aiofiles.open(str(path), "r", encoding="utf-8") as f:
            content = await f.read()
        for line in content.splitlines():
            line = line.strip()
            if line:
                messages.append(json.loads(line))
        return messages

    async def _repair_jsonl(self, path: Path) -> list[dict[str, Any]]:
        """Line-by-line recovery: skip individual corrupted lines."""
        messages: list[dict[str, Any]] = []
        async with aiofiles.open(str(path), "r", encoding="utf-8") as f:
            async for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    # Single corrupted line — skip, keep the rest
                    pass
        return messages
