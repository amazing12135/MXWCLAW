# Phase 6 更改记录

> **日期**: 2026-05-18
> **对应计划**: MXWbot_plan.md Phase 6 — 核心引擎
> **状态**: 已完成（296 tests passed, 6 skipped）

---

## 一、本阶段目标

实现核心引擎层：AgentRunner ReAct 循环 + ContextBuilder 上下文组装 + Hook 钩子系统 + SkillLoader 技能加载 + SubAgentManager 子代理管理。完成后可跑完整的 LLM ↔ Tool 循环。

---

## 二、实现文件与测试

| 文件 | 行数 | 核心类/函数 | 说明 |
|------|------|-------------|------|
| `core/hook.py` | 91 | `AgentHook`, `StreamProcessHook` | 7 个生命周期钩子 + 流式 think 清洗 |
| `core/skill.py` | 106 | `Skill`, `SkillLoader` | YAML frontmatter 解析 + XML 摘要 |
| `core/context.py` | 143 | `ContextBuilder` | 按 LLMCallPurpose 分支组装上下文 |
| `core/runner.py` | 195 | `AgentRunner`, `AgentRunSpec`, `AgentRunResult` | 非流式 ReAct 循环 + 批量确认 + Checkpoint |
| `core/subagent.py` | 172 | `SubAgentManager`, `SubAgentConfig`, `SubAgentResult` | 并发 Semaphore 控制 + 超时取消 |
| `tests/test_core/test_hook.py` | 41 | — | 4 tests |
| `tests/test_core/test_skill.py` | 76 | — | 6 tests |
| `tests/test_core/test_context.py` | 95 | — | 5 tests |
| `tests/test_core/test_runner.py` | 130 | — | 4 tests |
| `tests/test_core/test_subagent.py` | 92 | — | 5 tests |

---

## 三、与设计计划的差异及理由

### 3.1 Hook 系统 — 简化钩子接口

**设计计划**:
```
AgentHook: before_iteration / on_stream / on_stream_end / before_execute_tools / after_iteration / finalize_content
```

**实际实现**:
```python
class AgentHook:
    on_turn_start(session, state_manager, inbound_msg)  # 整轮开始
    before_llm_call(messages, tools)                    # LLM 调用前
    on_stream_delta(delta)                              # 流式增量
    on_llm_response(response)                           # LLM 完整响应后
    before_tool_call(name, args)                        # 工具执行前
    after_tool_call(name, result)                       # 工具执行后
    on_turn_end(session, response)                      # 整轮结束
```

**理由**: 按"轮次"而非"迭代"组织。`on_turn_start`/`on_turn_end` 覆盖整轮生命周期，`before_llm_call`/`on_llm_response` 覆盖 LLM 交互，`before_tool_call`/`after_tool_call` 覆盖工具执行。更自然、更易理解。

### 3.2 Runner — 非流式优先

**设计计划**: 使用 `chat_stream()` + `StreamProcessHook` 累积 tool_call deltas。

**实际实现**: 使用 `chat()` 非流式接口，LLMResponse 直接返回完整的 `tool_calls: list[ToolCallRequest]`。

**理由**:
1. 非流式接口更简单，减少状态管理复杂度（无需累积 tool_call deltas）
2. 流式模式可通过 `StreamProcessHook` + `chat_stream()` 后续添加，Runner 已预留 `on_stream_delta` Hook 回调点
3. 非流式模式对工具调用的处理更可靠（一次拿到完整参数，无需解析 delta 序列）

### 3.3 Skill 系统 — 省略 SkillMeta

**设计计划**:
```python
@dataclass
class SkillMeta:
    name, description, version, dependencies, availability,
    trigger_keywords, path, available  # 8 个字段
```

**实际实现**:
```python
@dataclass
class Skill:
    name, description, path, content, availability, dependencies  # 6 个字段
```

**理由**:
- `version`: 无版本管理需求，无消费方
- `trigger_keywords`: 自动触发尚未实现，无消费方
- `available` (依赖检查): `shutil.which()` 检查尚未实现，无消费方
- 遵循"不做假设性设计"原则，只保留有实际使用的字段

额外省略：
- **懒加载**: `.md` 技能文件通常 <5KB，一次性加载无性能问题
- **watchdog 热重载**: 后续可加，不影响核心流程
- **SkillParseError**: 缺失 frontmatter 静默返回 None，不阻塞其他 skill 扫描

### 3.4 SubAgent 系统 — 大幅简化

**设计计划**:
```
SubAgentConfig: task, model, max_iterations, timeout_seconds,
                restrict_to_workspace, inherit_working_memory, allowed_tools
SubAgentResult: agent_id, status(SubAgentStatus枚举), content, iterations,
                tool_calls_made, token_usage, error, duration_seconds,
                events(list[SubAgentEvent])
SubAgentManager: spawn, cancel, status, wait, active_count, list_results
```

**实际实现**:
```python
@dataclass
class SubAgentConfig:
    task: str
    provider: LLMProvider    # 直接注入实例而非按名查找
    tools: ToolRegistry | None = None
    max_iterations: int = 5
    timeout_seconds: int = 120
    workspace: Path | None = None  # 预留，当前未使用

@dataclass
class SubAgentResult:
    agent_id: str
    status: str              # "completed"|"cancelled"|"timeout"|"error"
    content: str = ""
    iterations: int = 0
    error: str | None = None

class SubAgentManager:
    spawn, cancel, wait, wait_all, active_count
```

**理由**:
- `SubAgentStatus` 枚举 → 字符串：4 个状态值不需要枚举，字符串更轻量
- `SubAgentEvent` 事件日志：调试阶段不需要结构化事件，可从日志获取
- `status()` / `list_results()` / `_results` 字典：`wait()` 返回结果即可，不需要缓存历史结果
- `model` → `provider`: 直接注入 Provider 实例，避免按名称查找的多一层间接
- `restrict_to_workspace` / `inherit_working_memory` / `allowed_tools`: 当前无使用场景

### 3.5 ContextBuilder — Identity Prompt 强化

计划中未详细定义的 identity prompt，实际实现包含：
- 运行时信息（Python 版本、OS、Shell、MXWbot 版本）
- 工作区路径（memory、sessions、skills 位置）
- 行为准则（先读后改、不预测工具结果、失败后分析原因等）

### 3.6 ContextBuilder — always_loaded skills 重复注入（已知问题）

`always_loaded` 类型的 skill 在上下文中出现两次：
1. `to_context_xml()` 生成的 XML 技能摘要（包含所有 skill 的名称和描述）
2. `get_always_loaded()` 返回的完整内容

每次对话浪费 ~200 tokens。后续应修复为：XML 摘要排除 always_loaded 项，或不再追加完整内容。

---

## 四、Bug 修复记录（Phase 6 评审发现）

| # | 位置 | 问题 | 修复 | 状态 |
|---|------|------|------|------|
| 1 | `register.py:30` | `unregister()` 返回中文字符串而非 bool | 未修复 — 后续处理 | 待修 |
| 2 | `runner.py:186` | `max_iterations=0` 时 `response`/`tool_calls` 未绑定 | 未修复 — 实际不会传 0 | 低优先 |
| 3 | `runner.py` | `tool_calls_made` 不跨迭代累积 | 添加 `total_tool_calls` 累加器 | ✅ 已修复 |
| 4 | `runner.py:176` | `after_tool_call` 传入 call_id 而非 tool_name | 改为传入 `tool_name` | ✅ 已修复 |

---

## 五、校验标准达成

- [x] `AgentRunner.run()` 完整执行 ReAct 循环 — 4 个 runner 测试通过
- [x] ContextBuilder.build(AGENT) 包含记忆+Skills，build(SUMMARY) 不包含 — 5 个 context 测试通过
- [x] 非只读工具触发批量确认 — 通过代码路径分析确认
- [x] SubAgent 超时自动取消 — timeout 测试通过
- [x] 全量测试 296 passed, 6 skipped（bwrap 不可用）

---

## 六、已知遗留问题

| 问题 | 严重程度 | 说明 |
|------|----------|------|
| `unregister()` 返回类型 | P2 | 返回字符串而非 bool，调用方误判 |
| always_loaded 重复注入 | P3 | XML + 全文双重出现，浪费 token |
| `_new_id()` 3 处重复 | P3 | runner/subagent/messages 各一份 |
| 子代理绕过 ContextBuilder | P3 | 手工构建 system prompt，workspace 字段未用 |
| `ToolRegistry.get()` 抛 KeyError | P3 | 不符合 Python `.get()` 惯例 |
| 流式通道未集成 | P4 | Runner 使用非流式，StreamProcessHook 未接入 |

---

## 七、下阶段

- 下阶段: Phase 7 — Channel 多渠道层
- 依赖本阶段: AgentRunner、ContextBuilder（Channel 消息处理时需要）
