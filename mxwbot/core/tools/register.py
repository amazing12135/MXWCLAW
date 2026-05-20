"""工具注册中心 — 注册/查找/转换 OpenAI Schema。

缓存 + 排序：`get_definitions()` 返回稳定排序的工具列表，
保证发给 LLM 的顺序一致 → prompt cache 命中率更高。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mxwbot.core.tools.base import Tool, ToolResult


class ToolRegistry:
    """管理所有已注册的工具。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._cached_definitions: list[dict[str, Any]] | None = None

    # -- 注册 ---------------------------------------------------------------

    def register(self, tool: Tool) -> None:
        """注册一个工具（同名覆盖），同时清除 schema 缓存。"""
        self._tools[tool.name] = tool
        self._cached_definitions = None

    def unregister(self, name: str)->bool:
        """移除一个工具，同时清除 schema 缓存。

        Returns:
            True 表示成功移除，False 表示工具不存在。
        """
        if name in self._tools:
            self._tools.pop(name, None)
            self._cached_definitions = None
            return True
        else:
            return False

    # -- 查找 ---------------------------------------------------------------

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Tool:
        """获取指定工具，未注册抛 KeyError。"""
        return self._tools[name]

    def list_names(self) -> list[str]:
        """返回所有已注册工具名称。"""
        return list(self._tools)

    def list_readonly_names(self) -> list[str]:
        """返回所有只读工具名称。"""
        return [n for n, t in self._tools.items() if t.is_readonly]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    # -- Schema 导出（缓存 + 稳定排序）--------------------------------------

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        """从 OpenAI 格式或扁平格式中提取工具名。"""
        fn = schema.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str):
                return name
        name = schema.get("name")
        return name if isinstance(name, str) else ""

    def get_definitions(self) -> list[dict[str, Any]]:
        """返回稳定排序的工具定义列表。

        builtin 工具按名排序在前，MCP_ 前缀工具按名排序在后。
        结果被缓存，register() 时自动失效。
        """
        if self._cached_definitions is not None:
            return self._cached_definitions

        definitions = [t.to_openai_schema() for t in self._tools.values()]
        builtins: list[dict[str, Any]] = []
        mcp_tools: list[dict[str, Any]] = []
        for schema in definitions:
            if self._schema_name(schema).startswith("mcp_"):
                mcp_tools.append(schema)
            else:
                builtins.append(schema)

        builtins.sort(key=self._schema_name)
        mcp_tools.sort(key=self._schema_name)
        self._cached_definitions = builtins + mcp_tools
        return self._cached_definitions

    def to_openai_schema(self) -> list[dict[str, Any]]:
        """导出为 OpenAI function calling 格式（快捷方法）。"""
        return self.get_definitions()

    # -- 调用预处理 ---------------------------------------------------------

    def prepare_call(
        self,
        name: str,
        params: dict[str, Any],
    ) -> tuple[Tool | None, dict[str, Any], str | None]:
        """解析、cast、validate 一次工具调用。

        Args:
            name: 工具名。
            params: LLM 传入的原始参数。

        Returns:
            (tool, cast后的参数, 错误消息或None)。tool 为 None 表示未找到。
        """
        if not isinstance(params, dict):
            return None, params, (
                f"工具 '{name}' 的参数必须是 JSON 对象，实际类型: {type(params).__name__}"
            )

        tool = self._tools.get(name)
        if tool is None:
            return None, params, (
                f"工具 '{name}' 未找到。可用: {', '.join(self.tool_names)}"
            )

        cast_params = tool.cast_params(params)
        errors = tool.validate_params(cast_params)
        if errors:
            return tool, cast_params, (
                f"工具 '{name}' 参数校验失败: {'; '.join(errors)}"
            )
        return tool, cast_params, None

    # -- 执行 ---------------------------------------------------------------

    async def execute_batch(
        self,
        calls: list[dict[str, Any]],
    ) -> list[tuple[str, ToolResult]]:
        """批量执行工具调用。

        Args:
            calls: [{"id": "call_1", "name": "read_file", "arguments": {...}}].

        Returns:
            [(call_id, ToolResult), ...]
        """
        tasks: list[asyncio.Task[ToolResult]] = []
        call_ids: list[str] = []

        for c in calls:
            cid = c.get("id", "")
            name = c.get("name", "")
            args = c.get("arguments", {})

            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    tasks.append(asyncio.create_task(
                        _error(f"工具 '{name}' 参数 JSON 解析失败")
                    ))
                    call_ids.append(cid)
                    continue

            _tool, cast_args, error = self.prepare_call(name, args)
            if error:
                tasks.append(asyncio.create_task(_error(error)))
            else:
                tasks.append(asyncio.create_task(_tool.execute(**cast_args)))  # type: ignore[union-attr]
            call_ids.append(cid)

        results = await asyncio.gather(*tasks)
        return list(zip(call_ids, results))


async def _error(msg: str) -> ToolResult:
    return ToolResult(success=False, error=msg)
