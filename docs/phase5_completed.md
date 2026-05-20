# Phase 5 完成报告

> **完成日期**: 2026-05-13
> **实际耗时**: 1 天
> **对应计划**: MXWbot_plan.md Phase 5

---

## 一、本阶段目标

工具子系统：抽象基类 + 注册中心 + 6 种核心工具 + 安全边界。

---

## 二、完成的项目结构

```
mxwbot/core/tools/
├── __init__.py          # 导出 Tool / ToolResult / ToolRegistry
├── base.py              # Tool ABC + cast/validate + JSON Schema 校验引擎
├── register.py          # ToolRegistry: register/unregister/get/prepare_call/execute_batch/get_definitions
├── filesystem.py        # 6 文件工具: Read/Write/Edit/ListDir/Glob/Grep
├── shell.py             # ShellTool: deny_patterns→注入→workspace边界→sandbox→进程管理
├── sandbox.py           # 沙箱后端注册表 + _bwrap() 命令包装 + SandboxTool
├── web.py               # WebSearchTool (DuckDuckGo) + WebFetchTool (httpx)
├── cron.py              # CronTool: add/list/remove + 时间解析 + JSONL 持久化
└── mcp.py               # McpTool 存根

tests/test_tools/
├── test_base.py         # Tool ABC + ToolResult 测试
├── test_registry.py     # ToolRegistry 注册/注销/查找/schema/execute_batch
├── test_filesystem.py   # 6 文件工具 + 路径穿越检测
├── test_shell.py        # ShellTool 安全检测 + 执行(skip if no bwrap)
├── test_sandbox.py      # 沙箱后端注册表 + bwrap 命令构建
├── test_web.py          # WebFetchTool mock 测试
└── test_cron.py         # CronTool 时间解析 + 增删查
```

---

## 三、核心设计要点

### 1. cast → validate 两阶段参数处理

LLM 常输出字符串 `"123"` 而非整数 `123`。`cast_params()` 先宽松转换，`validate_params()` 再严格校验，错误带完整路径（`config.timeout[0]`）。

### 2. 事件驱动注册中心

`get_definitions()` 排序+缓存：builtin 在前 MCP 在后，各自按名排序→保证 LLM prompt cache 命中率。`register()`/`unregister()` 自动清缓存。`prepare_call()` 一次完成 resolve→cast→validate。

### 3. Shell 纵深防御管线

```
allow_patterns 优先 → deny_patterns(13条) → detect_command_injection → workspace边界检查
  → sandbox.wrap_command("bwrap", cmd) → _spawn() → timeout kill + WNOHANG
```

不降级：bwrap 不可用 → 拒绝执行。

### 4. 沙箱可插拔架构

```python
_BACKENDS = {"bwrap": _bwrap}

def wrap_command(backend, command, ws, cwd) -> str:
    return _BACKENDS[backend](command, ws, cwd)
```

后续加 `firejail`/`docker` 只需 `_BACKENDS["firejail"] = _firejail`。`--tmpfs parent` 掩藏 config 等敏感父目录文件。

### 5. 文件系统工具路径安全

6 个工具全部通过 `is_path_safe()` 做 workspace 边界校验，覆盖 URL 编码、双写、semicolon 等 15 种路径穿越变种。

---

## 四、校验标准达成情况

- [x] Tool 基类 + cast/validate 正确
- [x] ToolRegistry register/unregister/get/to_openai_schema 往返
- [x] 文件系统工具路径穿越检测生效
- [x] Shell 工具 deny_patterns + 注入 + workspace 边界
- [x] Sandbox 后端注册表 + bwrap 可插拔
- [x] Cron 工具 add/list/remove + 持久化
- [x] 272 测试通过, 6 skipped (bwrap 未安装)

---

## 五、下阶段准备

- 下阶段: Phase 6 — 核心引擎
- 需要本阶段产出的: `core/tools/` (全部工具), `providers/` (LLM 调用), `session/` (历史管理)
- 状态: ✅ 就绪
