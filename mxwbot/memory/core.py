"""MemoryManager — 统一记忆调度入口。

掌管长期记忆存储并提供上下文组装和后台合并的单一入口。

Purpose 隔离:
  * 摘要器调用使用 purpose=SUMMARY → 最小上下文（不注入记忆）
  * Agent 调用  使用 purpose=AGENT   → 完整上下文（长期记忆 + 对话历史）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mxwbot.memory.long_term_memory import LongTermMemory
from mxwbot.memory.summarizer import MemorySummarizer
from mxwbot.memory.update import MemoryUpdater
from mxwbot.providers.base import LLMProvider


class MemoryManager:
    """统一记忆入口。

    Args:
        workspace: 运行时根目录。
        provider: 用于摘要生成的 LLM 供应商。
    """

    _COMPACT_USAGE_RATIO: float = 0.8
    _COMPACT_MSG_COUNT: int = 20

    def __init__(self, workspace: Path, provider: LLMProvider) -> None:
        db_path = workspace / "memory" / "memory.db"
        self._ltm = LongTermMemory(db_path)
        self._summarizer = MemorySummarizer(provider)
        self._updater = MemoryUpdater(self._ltm, self._summarizer)
        self._initialised = False

    async def init_db(self) -> None:
        """初始化 SQLite 表结构。构造后必须调用一次。"""
        await self._ltm.init_db()
        self._initialised = True

    async def get_context(self, query: str | None = None) -> str:
        """生成可注入 LLM system prompt 的记忆文本块。

        Args:
            query: 可选的搜索关键词。

        Returns:
            格式化的记忆文本，无结果时返回空字符串。
        """
        return await self._ltm.context_for_query(query)

    async def consolidate(
        self,
        messages: list[dict[str, Any]],
        consolidated_count: int,
        *,
        usage_ratio: float = 0.0,
        msg_count: int = 0,
    ) -> tuple[int, str]:
        """执行情景压缩 + 长期记忆存储。

        在每次 turn 结束时调用（State → DONE）。

        Args:
            messages: 当前会话消息列表。
            consolidated_count: 已压缩的消息数。
            usage_ratio: 当前 token 使用率。
            msg_count: 当前消息数。

        Returns:
            (新的已压缩计数, 压缩摘要文本)。
        """
        actual_count = msg_count or len(messages)

        summary = ""
        # 条件触发：token 紧张且消息足够多时，压缩前半段旧消息为摘要
        if usage_ratio > self._COMPACT_USAGE_RATIO and actual_count > self._COMPACT_MSG_COUNT:
            unconsolidated = messages[consolidated_count:]
            if len(unconsolidated) > 1:
                split = max(1, len(unconsolidated) // 2)
                summary = await self._summarizer.summarise_session(unconsolidated[:split])
                consolidated_count += split

        # 从最近消息中提取并存储长期记忆
        recent = messages[consolidated_count:] if consolidated_count else messages
        if recent:
            await self._updater.extract_and_store(recent)

        return consolidated_count, summary

    async def force_consolidate(
        self,
        messages: list[dict[str, Any]],
        consolidated_count: int,
    ) -> tuple[int, str]:
        """强制压缩所有未压缩消息 + 提取长期记忆。

        会话结束时调用，跳过 usage_ratio 和 msg_count 守卫。

        Args:
            messages: 完整消息列表。
            consolidated_count: 已压缩计数。

        Returns:
            新的已压缩计数。
        """
        summary = ""
        unconsolidated = messages[consolidated_count:]
        if len(unconsolidated) > 1:
            split = max(1, len(unconsolidated) // 2)
            summary = await self._summarizer.summarise_session(unconsolidated[:split])
            consolidated_count += split

        recent = messages[consolidated_count:] if consolidated_count else messages
        if recent:
            await self._updater.extract_and_store(recent)

        return consolidated_count, summary

    async def add_memory(self, content: str, category: str = "fact", importance: float = 0.5) -> str:
        """手动添加一条记忆。

        Args:
            content: 记忆内容。
            category: 分类标签。
            importance: 重要性评分。

        Returns:
            分配的记忆 id。
        """
        return await self._ltm.add({
            "content": content,
            "category": category,
            "importance": importance,
        })
