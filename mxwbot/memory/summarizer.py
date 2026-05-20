"""记忆摘要器 — 通过 LLM 提取结构化记忆。

使用 LLMCallPurpose.SUMMARY 防止递归：
摘要器自身的 LLM 调用不会再次触发记忆注入。
"""

from __future__ import annotations

import json
from typing import Any

from mxwbot.providers.base import LLMCallPurpose, LLMProvider, LLMResponse

_MEMORY_EXTRACTION_PROMPT = """从以下对话中提取值得长期记住的关键信息。

返回 JSON 数组，每个元素包含：
  - "content":  一条简洁的事实（一句话）
  - "category": 分类标签，可选值：user_profile / preference / project_info / decision / fact / other
  - "importance": 重要性评分 0.0–1.0

规则：
  - 忽略无意义的寒暄和琐碎信息（importance < 0.3 的不要返回）
  - 不要提取：密码、API Key、凭证、任何敏感数据
  - 只输出 JSON 数组，不要其他文字

对话：
{conversation}

JSON:"""

_SESSION_SUMMARY_PROMPT = """用 2–4 句话总结以下对话。聚焦主要话题、决策和待办事项。

{conversation}

摘要:"""


class MemorySummarizer:
    """使用 LLM 提取结构化记忆或压缩对话摘要。"""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def extract_facts(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """从对话消息中提取结构化记忆条目。

        Args:
            messages: 对话消息列表。

        Returns:
            记忆条目列表，每项含 content / category / importance。
        """
        conversation = self._messages_to_text(messages)
        if not conversation.strip():
            return []

        prompt = _MEMORY_EXTRACTION_PROMPT.format(conversation=conversation)
        response = await self._provider.chat(
            messages=[{"role": "user", "content": prompt}],
        )

        return self._parse_json_response(response)

    async def summarise_session(self, messages: list[dict[str, Any]]) -> str:
        """将对话压缩为一段短文摘要。

        Args:
            messages: 对话消息列表。

        Returns:
            2–4 句话的摘要文本。
        """
        conversation = self._messages_to_text(messages)
        if not conversation.strip():
            return ""

        prompt = _SESSION_SUMMARY_PROMPT.format(conversation=conversation)
        response = await self._provider.chat(
            messages=[{"role": "user", "content": prompt}],
        )

        return (response.content or "").strip()

    # -- 内部工具 -----------------------------------------------------------

    @staticmethod
    def _messages_to_text(messages: list[dict[str, Any]]) -> str:
        """将消息列表格式化为 LLM 可读的文本。

        Args:
            messages: 消息列表。

        Returns:
            格式化的文本，每行一条消息。
        """
        lines: list[str] = []
        for m in messages:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            if content:
                lines.append(f"[{role}]: {content}")
        return "\n".join(lines)

    @staticmethod
    def _parse_json_response(response: LLMResponse) -> list[dict[str, Any]]:
        """从 LLM 响应中解析 JSON 数组。

        自动处理 ```json ... ``` 代码围栏。

        Args:
            response: LLM 返回的响应。

        Returns:
            解析后的记忆条目列表，解析失败返回空列表。
        """
        text = (response.content or "").strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
            return []
        except json.JSONDecodeError:
            return []
