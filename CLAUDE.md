# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test Commands

```bash
# Create venv + install (first time)
uv venv
uv pip install -e ".[dev]"

# Run all tests
.venv/Scripts/python.exe -m pytest tests/ -v

# Run a single test file
.venv/Scripts/python.exe -m pytest tests/test_core/test_runner.py -v

# Run a single test function
.venv/Scripts/python.exe -m pytest tests/test_core/test_runner.py::TestAgentRunner::test_one_tool_call_cycle -v

# Install new dependency
uv pip install <package>
```

## Project Progress (10/10 Phases)

| Phase | Status | Key Modules |
|-------|--------|-------------|
| 1 骨架 | ✅ | `config/`, `utils/`, `bus/messages.py` |
| 2 Provider | ✅ | `providers/base.py`, `openai/anthropic/deepseek_provider.py` |
| 3 基础设施 | ✅ | `core/state.py`, `bus/queue.py`, `session/manager.py`, `checkpoint/manager.py` |
| 4 记忆 | ✅ | `memory/core.py`, `long_term_memory.py`, `summarizer.py`, `update.py` |
| 5 工具 | ✅ | `core/tools/{base,register,filesystem,shell,sandbox,web,cron}.py` |
| 6 引擎 | ✅ | `core/{hook,skill,context,runner,subagent}.py` |
| 7 Channel | ✅ | `channel/{base,weixin,qq,email}.py` |
| 8 编排 | ✅ | `core/loop.py` (LoopPool + Loop + LoopContext) |
| 9 服务 | ✅ | `system/`, `cron/`, `heartbeat/`, `watch/`, `cli/` |
| 10 测试文档 | ✅ | 覆盖率 > 80% |

## Architecture Overview

### Data Flow (per turn)
```
Channel → Bus.publish_inbound(InboundMessage)
  → LoopPool._consume()
    ├── confirmation_response → bus.resolve_confirmation()
    └── message → asyncio.create_task(_dispatch())
      → [Semaphore(20) + session Lock]
      → Loop.run(ctx)
        → StateManager: COMMAND→RESTORE→COMPACT→BUILD→RUN→SAVE→RESPOND→DONE
        → ContextBuilder.build(purpose, session, user_msg, memory, skills)
        → AgentRunner.run_stream(spec, messages)  [LLM ↔ Tool calls, 流式→Bus]
        → SessionManager.save_inbound()
        → MemoryManager.consolidate()
        → Bus.publish_stream_delta(is_end=True)  ← 关闭流
      → Bus.publish_outbound(OutboundMessage) → Channel.send()
```

### State Machine (event-driven)
Handlers return event strings (`"ok"`, `"error"`, `"shortcut"`, `"dispatch"`). A transition table maps `(state, event)` → `next_state`. Handlers never call `transition()` directly.
Table: `core/state.py:_TRANSITIONS`

### Loop Orchestrator
- `LoopPool`: 长驻消费者, Semaphore(20) 并发, per-session `asyncio.Lock` 串行, `stop()` 1s 内退出
- `Loop`: 每消息瞬态编排器, 注入 8 个 handler, `LoopContext` dataclass 传递状态
- `LoopContext`: `msg, session, session_key, messages, result, summary, checkpoint_restored, stop_requested, msg_count_before_run`
- `/clear /stop /status` 命令短路: COMMAND→shortcut→SAVE, 跳过 RESTORE...RUN
- Runner 错误: RUN→error→RESTORE 回退, emergency checkpoint 保存
- 流式: `AgentRunner.run_stream()` → `BusStreamHook` → `bus.publish_stream_delta()` → `Channel.send_stream()`
- RESPOND: 发送 `StreamDelta(is_end=True)` 关闭流 + `OutboundMessage` fallback
- `_finish`: /stop close session → prune checkpoint → fact extraction (无重复 compact)

### Heartbeat + Cron (Phase 9)
- `HeartbeatService`: LLM 驱动, 周期性读 workspace/HEARTBEAT.md, 通过 heartbeat tool 让 LLM 决策 skip/run
- `CronService`: at/every/cron 三种调度, JSON 持久化到 `cron/jobs.json`, timer sleep 到点触发
- `CronTool` (LLM 调用) 直接写 `cron/jobs.json`, CronService 检测 mtime 变化自动 reload
- `SystemManager`: 全局组件注册中心 + 拓扑排序启动, `ManagementAPI` 监听 127.0.0.1:9090
- CLI: Typer 子命令 (serve/channel/heartbeat/cron/watch/config/skill), 通过 HTTP 与 serve 通信

### Session
- Short-term: `Session.messages` (JSONL file per `channel:chat_id`)
- `Session.get_history()`: pipeline of user-turn alignment → orphan removal → sanitize → token trim
- `Session.consolidated_count`: pointer for compressed/uncompressed split
- `SessionManager.save_inbound()` converts InboundMessage → `{"role":"user", ...}`
- `Session.append_message()` for assistant/tool messages (LLM-compatible format)

### Memory
- Long-term: `LongTermMemory` (SQLite + FTS5), via `MemoryManager`
- `MemoryManager.get_context(query)` → FTS5 keyword search → prompt injection
- `MemoryManager.consolidate()`: usage_ratio > 0.8 → LLM compress old msgs + extract facts
- `MemorySummarizer` uses `LLMCallPurpose.SUMMARY` (minimal context, no memory recursion)

### Provider
- `LLMProvider.chat(messages, tools) → LLMResponse` (includes `LLMErrorInfo` on failure)
- `LLMProvider.chat_with_retry()`: retries on transient errors (1s→2s→4s backoff)
- `LLMResponse.is_ok` property, `TokenUsage(input_tokens, output_tokens)`
- Exceptions never propagate: `_safe_chat()` converts all to `LLMResponse(error=...)`

### Tools
- `Tool.execute(**kwargs) → ToolResult(success, content, error)`
- `ToolRegistry.prepare_call(name, params)`: resolve → cast → validate in one call
- `ToolRegistry.get_definitions()`: sorted + cached (builtin first, MCP_* last)
- `cast_params()`: string→int/bool coercion (LLM outputs strings)
- `validate_params()`: recursive JSON Schema with path tracking
- `ShellTool`: deny_patterns → injection → workspace boundary → sandbox.wrap_command()
- `SandboxTool`: pluggable backends (`_BACKENDS = {"bwrap": _bwrap}`)
- `_bwrap()`: `--tmpfs parent --dir ws --bind ws ws --ro-bind-try` for config masking

### Key Design Conventions
- **No unnecessary classes**: pure functions preferred unless state is needed (see `config/path.py`, `memory/token_budget.py`)
- **ENV override**: `MXWBOT_` prefix, `__` double-underscore for nesting, single underscores preserved
- **ChannelConfig**: generic `{type, enabled, settings}` dict, not hardcoded per-channel classes
- **OutboundMessage**: no independent `id`, identity is `reply_to` + `request_id`
- **Session lifecycle**: `close()` → `session.clear()` → remove from cache; JSONL persists on disk
- **Always delegate sandbox**: ShellTool never falls back to direct execution; no bwrap → error
- **Exceptions → LLMResponse.error**: callers check `resp.is_ok`, not try/except

---

## Phase 7 Security & Maintainability Bug Fixes (✅ Implemented)

> 目标：修复 Phase 7 channel 实现的安全漏洞和可维护性问题（高危+中危，不含低危和性能问题）

### Pre-Phase: 安全工具函数 (`mxwbot/utils/security.py`)

**P0.1 SSRF URL 校验函数**
- 新增 `_PRIVATE_IP_RANGES`: 定义私有/内网 IP 段常量（`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, `127.0.0.0/8`, `::1`, `fc00::/7`）
- 新增 `_is_private_host(hostname: str) -> bool`: DNS 解析 hostname 后检查是否命中私有 IP 段
- 新增 `validate_url(target: str) -> str`: 校验 URL scheme 为 http/https，host 非私有地址，返回规范化 URL；不合法抛 `ValueError`

**P0.2 Token 加密函数**
- 新增 `_derive_machine_key() -> bytes`: 使用 `uuid.getnode()` + `hashlib.pbkdf2_hmac` 派生 32 字节 AES 密钥
- 新增 `encrypt_token(plaintext: str) -> str`: AES-GCM 加密（nonce(12B) + ciphertext → base64）; 若 `cryptography` 不可用则 fallback base64 混淆 + warning
- 新增 `decrypt_token(encrypted: str) -> str`: 逆向解密

**P0.3 路径沙箱校验**
- 新增 `validate_path_in_sandbox(path_str: str, allowed_dirs: list[Path]) -> Path`: resolve → 检查前缀在 allowed_dirs 内 → 返回 resolved Path；不在则抛 `ValueError`

---

### Phase 1: Base 类修改 (`mxwbot/channel/base.py`)

**1.1 [M3] 统一错误分类体系** (在 `BaseChannel` 前添加)
```python
class ChannelError(Exception):          # 基类
class ChannelTransientError(ChannelError):  # 可重试（网络波动、限流）
class ChannelFatalError(ChannelError):      # 不可恢复（配置错误）
class ChannelAuthError(ChannelError):       # 认证失败（token 过期）
```

**1.2 [S5] 确认机制精确匹配**
- `_pending_confirmations` 类型由 `dict[str, str]` 改为 `dict[str, list[str]]`（chat_id → request_id 列表，FIFO）
- `send()`: `_pending_confirmations.setdefault(chat_id, []).append(msg.request_id)`
- `on_message()`: `pending_list.pop(0)` 取最早的 pending request（FIFO），列表空时删除 key
- fallback prompt 附带 `request_id[:8]` 供用户确认

**1.3 [M6] TTL Task 生命周期管理**
- `__init__` 新增 `self._ttl_tasks: dict[str, asyncio.Task]`
- `_schedule_confirmation_ttl` 改为返回 `asyncio.Task`，调用方存入 `_ttl_tasks`
- `send()`: 创建新 TTL 前 cancel 同 chat_id 的旧 task
- `stop()`: 遍历 `_ttl_tasks.values()` 全部 cancel 后再清理 pending

**1.4 [M2] 启动健康检查 API**
- 新增 `is_healthy() -> bool`: 返回 `self._running`
- 新增 `is_connected() -> bool`: 返回 `self._running`（子类覆写加入平台连接状态）

---

### Phase 2: WeChat 修复 (`mxwbot/channel/weixin.py`)

**2.1 [S1] Token 加密持久化**
- `_save_state()`: 将 `"token"` 字段改为 `"token_encrypted": encrypt_token(self._token)`；旧 `"token"` 字段删除
- `_load_state()`: 优先读 `token_encrypted` 并解密；fallback 读 `token`（兼容旧格式，读取后自动迁移加密）
- 导入 `from mxwbot.utils.security import encrypt_token, decrypt_token`

**2.2 [S6] 日志脱敏**
- 所有 `exc_info=True` 删除：行 157, 175, 305-306
- `_send_text` 错误日志：`chat_id` 只打印前 4 字符 `chat_id=%s...`
- `_api_post` 错误不在日志中暴露完整 errmsg

**2.3 [M2] `_start()` 启动失败反馈**
- token 缺失/为空时 `raise ChannelFatalError(...)` / `raise ChannelAuthError(...)` 代替 `return`
- 导入 `ChannelFatalError, ChannelAuthError`
- 覆写 `is_connected()`: `return self._running and self._client is not None and bool(self._token)`

**2.4 [M7] 消除冗余代码**
- 行 300: `data.get("msgs", []) or []` → `data.get("msgs") or []`（明确意图：key 不存在或值为 null/falsy 时用空列表）

---

### Phase 3: QQ 修复 (`mxwbot/channel/qq.py`)

**3.1 [S2] SSRF 防护**
- `_read_media_bytes` URL 分支：调用 `validate_url(media_ref)` 校验
- 关闭自动跳转 `follow_redirects=False`
- 手动跟踪 redirect（最多 5 跳），每跳调用 `validate_url` 校验目标，检测跳转环

**3.2 [S3] 路径遍历防护**
- `_read_media_bytes` 本地文件分支：`Path.resolve()` 后检查前缀在 `allowed_dirs` 内
- 越界路径记录 warning 并返回 `(None, None)`

**3.3 [S6] 日志脱敏**
- 删除 `exc_info=True`：行 150-152, 224, 260-263, 335
- `_send_text` 错误日志移除 chat_id 明文
- `_start` 日志 app_id 只展示前 8 字符（已有）

**3.4 [M1] 生命周期管理规范化**
- `__init__` 显式初始化 `self._http: httpx.AsyncClient | None = None`
- `_start()` 创建 `self._http`（不再在 `_read_media_bytes` 中懒加载）
- `_stop()` 移除 `hasattr(self, "_http")`，改为 `if self._http is not None`
- `_read_media_bytes` 移除懒创建逻辑，改为 `if self._http is None: return None, None`

**3.5 [M4] 消除闭包动态创建类**
- 删除 `_make_bot_class(channel)` 函数
- 新增模块级 `_QQBotClient(botpy.Client)` 类，通过构造参数接收 `on_c2c, on_group, on_direct` 回调
- `_start()` 中改为 `self._client = _QQBotClient(on_c2c=self._on_c2c_message, on_group=self._on_group_message, on_direct=self._on_c2c_message)`
- botpy 未安装时设 `_QQBotClient = None`

**3.6 [M2] `_start()` 启动失败反馈**
- SDK 缺失 → `raise ChannelFatalError(...)`
- 配置缺失 → `raise ChannelAuthError(...)`
- 覆写 `is_connected()`: `return self._running and self._client is not None`

---

### Phase 4: Email 修复 (`mxwbot/channel/email.py`)

**4.1 [S4] IMAP SSL 主机名校验**
- `_connect_imap` SSL 分支：创建 `ssl.create_default_context()` 传入 `IMAP4_SSL(..., ssl_context=ctx)`
- 自签名证书场景：catch `ssl.SSLError` 后 fallback 不校验 + warning（兼容性）

**4.2 [S6] 日志脱敏**
- `_poll_loop` 移除 `exc_info=True`
- `_send_text` 错误日志 to_addr 只打印前 4 字符 `to=%s...`
- 导入 `ssl`（已在文件顶部导入）

**4.3 [M5] IMAP 重试逻辑提取**
- 新增 `_imap_connection` 上下文管理器类（文件级，`EmailChannel` 前）
- `__enter__`: 2 次尝试重连（stale error 检测 → 重试），返回有效连接
- `__exit__`: 安全 logout
- `_fetch_unread()` 简化为 `with _imap_connection(self) as conn: return self._fetch_unread_once(conn)`

---

### Phase 5: 依赖声明修正 (`pyproject.toml`)

```toml
[project.optional-dependencies]
wechat = []           # httpx 已在 core 依赖中；+ cryptography (token 加密)
qq = ["qq-botpy>=1.0"]  # 实际使用 botpy，非 napcat
email = []             # 纯标准库
```

---

### Phase 6: 测试更新

**test_base.py:**
- S5: `test_confirmation_fifo_order_same_chat` — 同 chat 两个 confirmations，验证 FIFO 匹配
- S5: `test_confirmation_prompt_includes_request_id` — 验证 fallback_prompt 含 request_id
- M6: `test_ttl_task_cancelled_on_user_response` — 用户回复后 TTL task 被 cancel
- M6: `test_ttl_tasks_cleared_on_stop` — stop 后所有 TTL task 取消
- M3: `test_channel_error_hierarchy` — 验证异常继承关系

**test_weixin.py:**
- S1: `test_token_encrypted_in_state_file` — 验证 account.json 不包含明文 token
- S1: `test_token_decrypted_on_load` — 验证 _load_state 正确解密
- S1: `test_backward_compat_plaintext_token` — 旧格式 account.json 仍可读取
- M2: `test_start_raises_on_missing_token` — 验证抛 ChannelFatalError

**test_qq.py:**
- S2: `test_url_media_blocks_private_ip` — 内网 URL 被拒绝
- S3: `test_local_file_blocks_path_traversal` — `../../../etc/passwd` 被拒绝
- M1: `test_http_initialized_in_start` — `_http` 在 `_start()` 后非 None
- M4: `test_bot_created_with_callbacks` — `_QQBotClient` 使用回调构造

**test_email.py:**
- S4: `test_imap_ssl_context_passed` — 验证 `IMAP4_SSL` 收到 `ssl_context` 参数
- M5: `test_imap_connection_context_manager` — 上下文管理器正常获取/释放连接
- M5: `test_imap_connection_retry_on_stale` — stale error 触发重连

---

### 实施顺序 & 依赖关系

```
Phase 0 (security.py 新增工具函数)
  ↓
Phase 1 (base.py 异常体系 + TTL + 确认 + health API)
  ↓
Phase 2+3+4 (三个 Channel 修复，可并行)
  ↓
Phase 5 (pyproject.toml)
  ↓
Phase 6 (测试)
```

### 风险点

| 风险 | 缓解 |
|------|------|
| S1 token 加密后旧 account.json 无法读取 | `_load_state` fallback 读 `token` 明文字段，自动迁移 |
| S4 IMAP SSL 校验阻断自签名服务器 | catch `ssl.SSLError` fallback + warning |
| M4 `_QQBotClient` 在 botpy 未安装时不可用 | `except ImportError` 分支设 `_QQBotClient = None` |
| S5 确认 FIFO 与现有调用方行为不一致 | `_pending_confirmations` 列表为空时行为等价于旧版 |
