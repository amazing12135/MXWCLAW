"""Tool 抽象基类 + 结果数据结构 + JSON Schema 校验引擎。

设计借鉴 nanobot/cast/validate 两层模型：
  1. cast_params: LLM 常把数字/布尔输出为字符串，先宽松转换为目标类型
  2. validate_params: 按 JSON Schema 递归校验，错误带完整路径
"""

from __future__ import annotations

import copy
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """工具执行结果。"""

    success: bool = True
    content: str = ""
    error: str | None = None


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def resolve_json_type(schema: dict[str, Any]) -> str | None:
    """从 JSON Schema 的 type 字段中提取非 null 类型。

    处理 ``["string", "null"]`` 这种联合类型。
    """
    t = schema.get("type")
    if isinstance(t, list):
        for item in t:
            if item != "null":
                return item
        return t[0] if t else None
    return t


def subpath(path: str, key: str) -> str:
    """构建嵌套路径，如 ``config.timeout``。"""
    return f"{path}.{key}" if path else key


def validate_json_schema(
    value: Any,
    schema: dict[str, Any],
    path: str = "",
) -> list[str]:
    """按 JSON Schema 递归校验值，返回错误列表（空 = 通过）。

    Args:
        value: 待校验的值。
        schema: JSON Schema fragment。
        path: 当前路径（用于错误定位）。

    Returns:
        错误消息列表。
    """
    errors: list[str] = []

    json_type = resolve_json_type(schema)
    nullable = schema.get("nullable", False) or (
        isinstance(schema.get("type"), list) and "null" in schema.get("type", [])
    )

    # null check
    if value is None:
        if not nullable:
            errors.append(f"{path}: 值为 null 但 schema 不允许")
        return errors

    # type check
    if json_type in JSON_TYPE_MAP:
        expected = JSON_TYPE_MAP[json_type]
        if not isinstance(value, expected):
            errors.append(
                f"{path}: 期望 {json_type}，实际 {type(value).__name__}"
            )
            return errors

    if json_type == "string":
        min_len = schema.get("minLength")
        max_len = schema.get("maxLength")
        if min_len is not None and len(value) < min_len:
            errors.append(f"{path}: 字符串长度 {len(value)} < {min_len}")
        if max_len is not None and len(value) > max_len:
            errors.append(f"{path}: 字符串长度 {len(value)} > {max_len}")

    elif json_type in ("integer", "number"):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            errors.append(f"{path}: {value} < {minimum}")
        if maximum is not None and value > maximum:
            errors.append(f"{path}: {value} > {maximum}")

    elif json_type == "array":
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if min_items is not None and len(value) < min_items:
            errors.append(f"{path}: 数组长度 {len(value)} < {min_items}")
        if max_items is not None and len(value) > max_items:
            errors.append(f"{path}: 数组长度 {len(value)} > {max_items}")
        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(value):
                errors.extend(
                    validate_json_schema(item, items_schema, f"{path}[{i}]")
                )

    elif json_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}: 缺少必需字段 {key!r}")
        for key, val in value.items():
            if key in properties:
                errors.extend(
                    validate_json_schema(val, properties[key], subpath(path, key))
                )

    # enum
    enum_vals = schema.get("enum")
    if enum_vals is not None and value not in enum_vals:
        errors.append(f"{path}: {value!r} 不在枚举 {enum_vals} 中")

    return errors


# ---------------------------------------------------------------------------
# Cast helpers — 类型强制转换
# ---------------------------------------------------------------------------

def cast_value(value: Any, schema: dict[str, Any]) -> Any:
    """宽松类型转换：将 LLM 输出的字符串转为目标类型。"""
    json_type = resolve_json_type(schema)
    if json_type is None:
        return value

    if json_type == "integer" and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    if json_type == "number" and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    if json_type == "boolean" and isinstance(value, str):
        if value.lower() in ("true", "1", "yes"):
            return True
        if value.lower() in ("false", "0", "no"):
            return False
        return value
    if json_type == "string" and not isinstance(value, str):
        return str(value)

    if json_type == "object" and isinstance(value, dict):
        properties = schema.get("properties", {})
        return {
            k: cast_value(v, properties.get(k, {}))
            for k, v in value.items()
        }

    if json_type == "array" and isinstance(value, list):
        items = schema.get("items", {})
        return [cast_value(v, items) for v in value]

    return value


# ---------------------------------------------------------------------------
# Tool ABC
# ---------------------------------------------------------------------------

class Tool(ABC):
    """工具抽象基类。

    子类必须定义:
      - name: 工具名
      - description: 工具描述
      - parameters: JSON Schema (dict)
      - execute(**kwargs): 异步执行方法

    可选:
      - is_readonly: 无副作用（默认 False）
      - exclusive: 需独占执行（默认 False）
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    is_readonly: bool = False
    exclusive: bool = False

    @property
    def concurrency_safe(self) -> bool:
        """能否与其他并发安全的工具同时执行。"""
        return self.is_readonly and not self.exclusive

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """执行工具逻辑。"""

    # -- 参数预处理 ---------------------------------------------------------

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """宽松类型转换：LLM 常输出字符串，先转为正确类型再校验。

        Args:
            params: LLM 传入的原始参数字典。

        Returns:
            转换后的参数字典。
        """
        if not isinstance(params, dict):
            return params
        return cast_value(params, self.parameters)  # type: ignore[return-value]

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """按 JSON Schema 校验参数。

        Args:
            params: cast 后的参数字典。

        Returns:
            错误列表，空列表表示校验通过。
        """
        if not isinstance(params, dict):
            return ["参数必须是字典"]
        return validate_json_schema(params, self.parameters)

    # -- Schema 导出 --------------------------------------------------------

    def to_openai_schema(self) -> dict[str, Any]:
        """导出为 OpenAI function calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(self.parameters),
            },
        }


# ---------------------------------------------------------------------------
# Meta-tool: get_tool_schema — lazy-load extension tool definitions
# ---------------------------------------------------------------------------

class GetToolSchemaTool(Tool):
    """元工具：让 LLM 按需获取扩展工具的完整 JSON Schema。

    首次调用扩展工具前，LLM 调用 ``get_tool_schema("tool_name")``
    获取其完整参数定义。获取后该工具在本次 session 内可直接使用。
    """

    name = "get_tool_schema"
    description = "获取指定工具的完整参数定义。首次使用扩展工具前调用。"
    is_readonly = True
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "要获取参数定义的工具名称",
            },
        },
        "required": ["name"],
    }

    def __init__(self, registry: Any = None) -> None:
        self._registry = registry

    async def execute(self, name: str, **kwargs: Any) -> ToolResult:
        if self._registry is None:
            return ToolResult(success=False, error="ToolRegistry 未注入")
        try:
            tool = self._registry.get(name)
        except KeyError:
            return ToolResult(success=False, error=f"工具不存在: {name}")
        schema = tool.to_openai_schema()
        return ToolResult(content=json.dumps(schema, ensure_ascii=False, indent=2))


# Sentinel for unset registry
_GET_TOOL_SCHEMA: GetToolSchemaTool | None = None


def get_tool_schema_instance(registry: Any = None) -> GetToolSchemaTool:
    """Return the singleton GetToolSchemaTool, injecting registry if provided."""
    global _GET_TOOL_SCHEMA
    if _GET_TOOL_SCHEMA is None:
        _GET_TOOL_SCHEMA = GetToolSchemaTool(registry)
    elif registry is not None and _GET_TOOL_SCHEMA._registry is None:
        _GET_TOOL_SCHEMA._registry = registry
    return _GET_TOOL_SCHEMA


# Names of tools always sent with full schema (core set).
CORE_TOOL_NAMES = frozenset({
    "read_file", "write_file", "edit_file", "list_dir",
})
