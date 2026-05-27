"""测试 core/tools/base.py 和 core/tools/register.py

覆盖 cast/validate/prepare_call/execute_batch 全链路。
"""

import pytest

from mxwbot.core.tools.base import (
    Tool,
    ToolResult,
    cast_value,
    validate_json_schema,
    resolve_json_type,
    subpath,
)
from mxwbot.core.tools.register import ToolRegistry


# ============================================================================
# 辅助：一个带完整 JSON Schema 的测试工具
# ============================================================================

class _FileReadTool(Tool):
    name = "read_file"
    description = "读取文件内容"
    is_readonly = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "max_lines": {
                "type": "integer",
                "description": "最大行数",
                "minimum": 1,
                "maximum": 1000,
            },
            "encoding": {
                "type": "string",
                "enum": ["utf-8", "gbk", "latin-1"],
            },
        },
        "required": ["path"],
    }

    async def execute(self, **kwargs):
        return ToolResult(content=f"read: {kwargs.get('path', '')}")


class _WriteFileTool(Tool):
    name = "write_file"
    description = "写入文件"
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, **kwargs):
        return ToolResult(content="written")


# ============================================================================
# resolve_json_type / subpath
# ============================================================================

class TestResolveJsonType:
    def test_simple_type(self):
        assert resolve_json_type({"type": "string"}) == "string"

    def test_nullable_union(self):
        assert resolve_json_type({"type": ["string", "null"]}) == "string"

    def test_no_type(self):
        assert resolve_json_type({}) is None

    def test_empty_union(self):
        assert resolve_json_type({"type": []}) is None


class TestSubpath:
    def test_root(self):
        assert subpath("", "key") == "key"

    def test_nested(self):
        assert subpath("config", "timeout") == "config.timeout"

    def test_deep(self):
        assert subpath("a.b", "c") == "a.b.c"


# ============================================================================
# cast_value — 类型强制转换
# ============================================================================

class TestCastValue:
    """cast_value: LLM 传字符串→转为 schema 声明的正确类型"""

    def test_string_passthrough(self):
        assert cast_value("hello", {"type": "string"}) == "hello"

    def test_int_to_string(self):
        """path=123, schema 是 string → str(123) → '123'"""
        assert cast_value(123, {"type": "string"}) == "123"

    def test_string_to_int(self):
        """max_lines='50', schema 是 integer → int('50') → 50"""
        assert cast_value("50", {"type": "integer"}) == 50

    def test_string_to_int_fail_keep_original(self):
        """max_lines='hello', schema 是 integer → int() 失败, 保留原值"""
        assert cast_value("hello", {"type": "integer"}) == "hello"

    def test_string_to_float(self):
        assert cast_value("3.14", {"type": "number"}) == 3.14

    def test_string_bool_true(self):
        schema = {"type": "boolean"}
        assert cast_value("true", schema) is True
        assert cast_value("1", schema) is True
        assert cast_value("yes", schema) is True

    def test_string_bool_false(self):
        schema = {"type": "boolean"}
        assert cast_value("false", schema) is False
        assert cast_value("0", schema) is False
        assert cast_value("no", schema) is False

    def test_bool_passthrough(self):
        assert cast_value(True, {"type": "boolean"}) is True
        assert cast_value(False, {"type": "boolean"}) is False

    def test_nested_object_cast(self):
        schema = {
            "type": "object",
            "properties": {
                "timeout": {"type": "integer"},
            },
        }
        result = cast_value({"timeout": "30"}, schema)
        assert result == {"timeout": 30}

    def test_array_cast(self):
        schema = {
            "type": "array",
            "items": {"type": "integer"},
        }
        result = cast_value(["1", "2", "3"], schema)
        assert result == [1, 2, 3]

    def test_no_type_returns_as_is(self):
        assert cast_value("abc", {}) == "abc"
        assert cast_value(42, {}) == 42

    def test_unparsable_bool_string_preserved(self):
        """非标准布尔字符串, cast 不做猜测, 保留原值"""
        assert cast_value("maybe", {"type": "boolean"}) == "maybe"


# ============================================================================
# validate_json_schema — 递归 JSON Schema 校验
# ============================================================================

class TestValidateJsonSchema:
    def test_valid_string(self):
        assert validate_json_schema("hello", {"type": "string"}) == []

    def test_type_mismatch(self):
        errors = validate_json_schema(42, {"type": "string"})
        assert len(errors) == 1
        assert "期望 string" in errors[0]
        assert "int" in errors[0]

    def test_null_not_allowed(self):
        errors = validate_json_schema(None, {"type": "string"})
        assert len(errors) == 1
        assert "null" in errors[0]

    def test_null_allowed(self):
        assert validate_json_schema(None, {"type": ["string", "null"]}) == []

    def test_nullable_true(self):
        assert validate_json_schema(None, {"type": "string", "nullable": True}) == []

    def test_min_length(self):
        assert validate_json_schema("ab", {"type": "string", "minLength": 3}) != []

    def test_max_length(self):
        assert validate_json_schema("abcde", {"type": "string", "maxLength": 3}) != []

    def test_integer_range(self):
        assert validate_json_schema(0, {"type": "integer", "minimum": 1}) != []
        assert validate_json_schema(5, {"type": "integer", "minimum": 0, "maximum": 10}) == []

    def test_enum_valid(self):
        assert validate_json_schema("utf-8", {"type": "string", "enum": ["utf-8", "gbk"]}) == []

    def test_enum_invalid(self):
        errors = validate_json_schema("ascii", {"type": "string", "enum": ["utf-8", "gbk"]})
        assert len(errors) == 1
        assert "枚举" in errors[0]

    def test_required_field_missing(self):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        errors = validate_json_schema({}, schema)
        assert len(errors) == 1
        assert "缺少必需字段" in errors[0]
        assert "name" in errors[0]

    def test_nested_validation_with_path(self):
        schema = {
            "type": "object",
            "properties": {
                "config": {
                    "type": "object",
                    "properties": {
                        "timeout": {"type": "integer"},
                    },
                },
            },
        }
        errors = validate_json_schema({"config": {"timeout": "abc"}}, schema)
        assert len(errors) == 1
        assert "config.timeout" in errors[0]

    def test_array_items_validation(self):
        schema = {
            "type": "array",
            "items": {"type": "integer"},
        }
        errors = validate_json_schema([1, "two", 3], schema)
        assert len(errors) == 1
        assert "[1]" in errors[0]

    def test_array_length(self):
        schema = {"type": "array", "minItems": 2}
        assert validate_json_schema([1], schema) != []

    def test_extra_field_not_rejected(self):
        """多余字段不拦截（设计选择）"""
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
        errors = validate_json_schema({"path": "/tmp/a.txt", "extra": "noise"}, schema)
        assert errors == []


# ============================================================================
# Tool.cast_params / Tool.validate_params
# ============================================================================

class TestToolCastValidate:
    def test_cast_params_coerces_types(self):
        tool = _FileReadTool()
        result = tool.cast_params({"path": 123, "max_lines": "50"})
        assert result["path"] == "123"
        assert result["max_lines"] == 50

    def test_cast_params_not_dict(self):
        tool = _FileReadTool()
        assert tool.cast_params("not_a_dict") == "not_a_dict"  # type: ignore

    def test_validate_params_valid(self):
        tool = _FileReadTool()
        assert tool.validate_params({"path": "/tmp/a.txt"}) == []

    def test_validate_params_missing_required(self):
        tool = _FileReadTool()
        errors = tool.validate_params({})
        assert len(errors) == 1
        assert "path" in errors[0]

    def test_validate_params_bad_type_after_cast_fail(self):
        """cast 失败保留原值 → validate 拦截"""
        tool = _FileReadTool()
        errors = tool.validate_params({"path": "ok", "max_lines": "hello"})
        # max_lines cast 失败, 保留 "hello" → validate 发现不是 integer
        assert len(errors) >= 1
        assert any("max_lines" in e for e in errors)

    def test_validate_params_enum_invalid(self):
        tool = _FileReadTool()
        errors = tool.validate_params({"path": "/tmp/a.txt", "encoding": "ascii"})
        assert len(errors) == 1
        assert "encoding" in errors[0]
        assert "枚举" in errors[0]

    def test_validate_params_range(self):
        tool = _FileReadTool()
        errors = tool.validate_params({"path": "/tmp/a.txt", "max_lines": 0})
        assert len(errors) >= 1
        assert "max_lines" in errors[0]

    def test_validate_params_not_dict(self):
        tool = _FileReadTool()
        errors = tool.validate_params([1, 2, 3])  # type: ignore
        assert len(errors) == 1
        assert "字典" in errors[0]


# ============================================================================
# ToolRegistry.prepare_call — 解析 → cast → validate 一站式
# ============================================================================

class TestPrepareCall:
    def test_valid_call(self):
        reg = ToolRegistry()
        reg.register(_FileReadTool())
        tool, params, error = reg.prepare_call("read_file", {"path": "/tmp/a.txt"})
        assert tool is not None
        assert error is None
        assert params["path"] == "/tmp/a.txt"

    def test_tool_not_found(self):
        reg = ToolRegistry()
        tool, params, error = reg.prepare_call("nonexistent", {})
        assert tool is None
        assert "未找到" in error
        assert "可用" in error

    def test_params_not_dict(self):
        reg = ToolRegistry()
        reg.register(_FileReadTool())
        tool, params, error = reg.prepare_call("read_file", "not_dict")
        assert tool is None
        assert "JSON 对象" in error

    def test_validation_error_message_format(self):
        """校验失败的错误消息包含工具名 + 具体原因"""
        reg = ToolRegistry()
        reg.register(_FileReadTool())
        tool, params, error = reg.prepare_call("read_file", {})
        assert tool is not None  # 工具找到了
        assert error is not None  # 但校验失败
        assert "read_file" in error
        assert "参数校验失败" in error
        assert "path" in error

    def test_cast_before_validate(self):
        """确保 cast 先于 validate 执行: 字符串数字→int 再校验 range"""
        reg = ToolRegistry()
        reg.register(_FileReadTool())
        _, params, error = reg.prepare_call(
            "read_file", {"path": "/tmp/a.txt", "max_lines": "50"}
        )
        assert error is None
        assert isinstance(params["max_lines"], int)
        assert params["max_lines"] == 50

    def test_missing_params_clear_error(self):
        """缺少必填参数时错误消息能帮 LLM 纠正"""
        reg = ToolRegistry()
        reg.register(_WriteFileTool())
        _, _, error = reg.prepare_call("write_file", {"path": "/tmp/a.txt"})
        assert error is not None
        assert "content" in error  # 明确告诉 LLM 缺了 content


# ============================================================================
# ToolRegistry.execute_batch — 批量执行
# ============================================================================

class TestExecuteBatch:
    @pytest.mark.asyncio
    async def test_all_success(self):
        reg = ToolRegistry()
        reg.register(_FileReadTool())
        calls = [
            {"id": "c1", "name": "read_file", "arguments": {"path": "/tmp/a.txt"}},
        ]
        results = await reg.execute_batch(calls)
        assert len(results) == 1
        cid, result = results[0]
        assert cid == "c1"
        assert result.success
        assert "read:" in result.content

    @pytest.mark.asyncio
    async def test_validation_error_returns_toolresult(self):
        """参数校验失败不崩，返回 ToolResult(success=False)"""
        reg = ToolRegistry()
        reg.register(_FileReadTool())
        calls = [
            {"id": "c1", "name": "read_file", "arguments": {}},
        ]
        results = await reg.execute_batch(calls)
        assert len(results) == 1
        _, result = results[0]
        assert not result.success
        assert "参数校验失败" in result.error

    @pytest.mark.asyncio
    async def test_tool_not_found_returns_error(self):
        reg = ToolRegistry()
        calls = [
            {"id": "c1", "name": "nonexistent", "arguments": {}},
        ]
        results = await reg.execute_batch(calls)
        _, result = results[0]
        assert not result.success
        assert "未找到" in result.error

    @pytest.mark.asyncio
    async def test_args_json_parse_failure(self):
        """arguments 是非法 JSON 字符串时优雅降级"""
        reg = ToolRegistry()
        reg.register(_FileReadTool())
        calls = [
            {"id": "c1", "name": "read_file", "arguments": "not valid json{{{ "},
        ]
        results = await reg.execute_batch(calls)
        _, result = results[0]
        assert not result.success
        assert "JSON 解析失败" in result.error

    @pytest.mark.asyncio
    async def test_mixed_results(self):
        """混合结果: 一个成功一个校验失败 → 都正常返回"""
        reg = ToolRegistry()
        reg.register(_FileReadTool())
        reg.register(_WriteFileTool())
        calls = [
            {"id": "c1", "name": "read_file", "arguments": {"path": "/tmp/a.txt"}},
            {"id": "c2", "name": "write_file", "arguments": {"path": "/tmp/b.txt"}},  # 缺 content
        ]
        results = await reg.execute_batch(calls)
        assert len(results) == 2
        success = [cid for cid, r in results if r.success]
        failures = [cid for cid, r in results if not r.success]
        assert "c1" in success
        assert "c2" in failures

    @pytest.mark.asyncio
    async def test_args_string_json(self):
        """arguments 是合法 JSON 字符串时正确解析"""
        reg = ToolRegistry()
        reg.register(_FileReadTool())
        calls = [
            {"id": "c1", "name": "read_file", "arguments": '{"path": "/tmp/a.txt"}'},
        ]
        results = await reg.execute_batch(calls)
        _, result = results[0]
        assert result.success


# ============================================================================
# ToolRegistry — 注册 / 查询 / Schema
# ============================================================================

class TestToolRegistry:
    def test_register_and_get(self):
        reg = ToolRegistry()
        tool = _FileReadTool()
        reg.register(tool)
        assert reg.get("read_file") is tool

    def test_get_nonexistent_raises(self):
        reg = ToolRegistry()
        with pytest.raises(KeyError):
            reg.get("nonexistent")

    def test_register_overwrites(self):
        reg = ToolRegistry()
        t1 = _FileReadTool()
        t2 = _FileReadTool()
        reg.register(t1)
        reg.register(t2)
        assert reg.get("read_file") is t2

    def test_get_definitions_sorted(self):
        reg = ToolRegistry()
        reg.register(_WriteFileTool())  # w
        reg.register(_FileReadTool())   # r
        defs = reg.get_definitions()
        names = [d["function"]["name"] for d in defs]
        assert names == ["read_file", "write_file"]  # 按名排序

    def test_unregister(self):
        reg = ToolRegistry()
        reg.register(_FileReadTool())
        assert reg.unregister("read_file") is True
        with pytest.raises(KeyError):
            reg.get("read_file")

    def test_unregister_nonexistent(self):
        reg = ToolRegistry()
        assert reg.unregister("nonexistent") is False

    def test_contains(self):
        reg = ToolRegistry()
        reg.register(_FileReadTool())
        assert "read_file" in reg
        assert "nonexistent" not in reg

    def test_cache_invalidated_on_register(self):
        reg = ToolRegistry()
        reg.register(_FileReadTool())
        d1 = reg.get_definitions()
        reg.register(_WriteFileTool())
        d2 = reg.get_definitions()
        assert len(d2) == len(d1) + 1
