"""记忆写入 — 重要性评分 + 去重 + 持久化。

通过摘要器提取结构化事实，去重后写入长期记忆存储。
"""

from __future__ import annotations

from typing import Any

from mxwbot.memory.long_term_memory import LongTermMemory
from mxwbot.memory.summarizer import MemorySummarizer


class MemoryUpdater:
    """决定哪些内容应持久化到长期记忆。

    Args:
        ltm: 长期记忆存储。
        summarizer: LLM 事实提取器。
        min_importance: 低于此分的条目被丢弃。
    """

    def __init__(
        self,
        ltm: LongTermMemory,
        summarizer: MemorySummarizer,
        *,
        min_importance: float = 0.4,
    ) -> None:
        self._ltm = ltm
        self._summarizer = summarizer
        self._min_importance = min_importance

    async def extract_and_store(self, messages: list[dict[str, Any]]) -> list[str]:
        """从消息中提取事实并持久化高重要性条目。

        Args:
            messages: 对话消息列表。

        Returns:
            成功存储的记忆 id 列表。
        """
        raw = await self._summarizer.extract_facts(messages)
        if not raw:
            return []

        # 按重要性过滤
        candidates = [
            r for r in raw
            if float(r.get("importance", 0)) >= self._min_importance
        ]
        if not candidates:
            return []

        # 去重：搜索已有记忆，跳过相似内容
        new_entries: list[dict[str, Any]] = []
        for c in candidates:
            content = str(c.get("content", ""))
            if not content.strip():
                continue
            existing = await self._ltm.search(content, k=1)
            if existing and self._is_duplicate(content, existing[0].get("content", "")):
                continue
            new_entries.append({
                "content": content,
                "category": str(c.get("category", "other")),
                "importance": float(c.get("importance", 0.5)),
            })

        if not new_entries:
            return []

        return await self._ltm.add_batch(new_entries)

    @staticmethod
    def _is_duplicate(a: str, b: str, threshold: float = 0.7) -> bool:
        """基于词重叠率的简单去重。

        Args:
            a: 待写入的内容。
            b: 已有记忆的内容。
            threshold: 重叠率阈值，超过即视为重复。

        Returns:
            True 表示两段内容被认为是重复的。
        """
        a_words = set(a.lower().split())
        b_words = set(b.lower().split())
        if not a_words or not b_words:
            return False
        intersection = a_words & b_words
        return len(intersection) / min(len(a_words), len(b_words)) > threshold
