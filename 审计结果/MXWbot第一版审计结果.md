# MXWbot 第一版设计审计结果

> **审计日期**: 2026-05-11
> **审计对象**: MXWbot_DESIGN_SPEC.md v1.0
> **审计视角**: 资深 Agent 系统开发者

---

## 总体评价

Spec 的设计骨架是好的——分层架构清晰、模块划分合理、检查点恢复、Token 预算管理、安全考虑都有体现。核心问题集中在**执行流细节的缺失**和**几个关键组件设计的空洞**。以下按优先级列出所有问题及修改建议。

---

## P0（阻塞开发）

### 1. 状态机顺序不合理 —— COMPACT 在 COMMAND 之前

**当前逻辑**:

```
RESTORE -> COMPACT -> COMMAND -> BUILD -> RUN -> SAVE -> RESPOND -> DONE
```

如果用户发 `/clear`，Token 压缩先执行，然后才判断命令。`/clear` 本身目的就是清空上下文，提前 compact 毫无意义且浪费计算。

**修改建议**:

把 COMMAND 提到 COMPACT 之前。命令命中后直接跳转到 SAVE/RESPOND，跳过 COMPACT 和 BUILD。

```
RESTORE -> COMMAND -> COMPACT -> BUILD -> RUN -> SAVE -> RESPOND -> DONE
              │
              └── 命令命中 ──→ RESPOND → DONE
```

Loop.run() 中的流程调整为:

```
1. 系统消息检测 → 特殊路径
2. 获取/创建 Session
3. 检查未恢复 Checkpoint → 从断点续跑
4. 斜杠命令分发 ← 先判命令
5. Token 预算判断是否需要 compact（超过阈值才执行）
6. 上下文构建
7. AgentRunner.run()
...
```

---

### 2. 并发模型完全未定义

整个 Spec 没有定义当多个用户（如 3 个微信 + 2 个 QQ）同时发消息时的处理方式。这是联网运行时必须面对的问题。

**需要明确**:

1. Loop 处理模型的二选一：
   - **A**: 全局串行——一次只处理一条消息（简单但延迟不可控）
   - **B**: 按 session 隔离并行——每个 `channel:chat_id` 独立序列化执行，不同 session 间异步并发（推荐）

2. 如果选方案 B，需要细化：
   - SessionManager 的 `asyncio.Lock` 需按 session_id 分离，非全局一把锁
   - CheckpointManager 的写入需按 session 分目录，避免并发冲突
   - MemoryManager 的 SQLite 需要 WAL 模式支持并发读写

3. Input Queue 的消费策略：
   - 单消费者 Loop 无法并发，建议 Loop 变为调度器，每个消息 spawn 一个协程（受并发限制器约束）

4. 全局并发上限：建议同时处理的 session 数上限（如 50），防止资源耗尽

---

### 3. 流式输出到 Channel 的路径缺失

Spec 定义了 Provider 的流式接口 `AsyncIterator[str]` 和 Hook 的 `on_stream`，但没有说明流式 chunk 如何通过 MessageBus 到达 Channel。

**问题分析**:

当前 MessageBus 是 `InboundMessage -> Loop -> OutboundMessage` 的单次往返模型。流式输出需要**多次增量推送**，与当前模型不兼容。

**修改建议**（二选一）:

**方案 A — MessageBus 支持流式**（推荐，保持解耦）:

在 `OutboundMessage` 中增加流式字段：

```python
@dataclass
class OutboundMessage:
    channel_id: str
    chat_id: str
    content: str
    reply_to: str | None

    # 流式字段
    is_streaming: bool = False
    stream_index: int = 0       # 递增序号
    is_stream_end: bool = False  # 最后一块
```

Channel 需要支持累积渲染，收到第一个 `is_streaming=True` 时开始显示，收到 `is_stream_end=True` 时完成。

**方案 B — Loop 持有 Channel 直接回调**（更高性能但引入耦合）:

Loop 在处理时持有当前 Channel 的流式回调引用，绕过 MessageBus 直接推送。这会破坏 Channel <-> Agent 的解耦，不推荐。

---

## P1（影响核心功能/数据安全）

### 4. 记忆系统存在递归依赖风险

**问题**:

`MemorySummarizer` 调用 LLM 生成摘要。但 LLM 本身依赖 `ContextBuilder.build_memory_context()` 注入长期记忆。存在以下风险：

- 用户请求 -> LLM -> 需要上下文 -> 长期记忆正在被摘要更新（状态不一致）
- 摘要 LLM 调用如果也触发工具调用，会形成无限递归
- 摘要 LLM 消耗的 Token 可能比压缩省下的还多

**修改建议**:

1. 摘要 LLM 调用使用独立的小模型（如 Claude Haiku / DeepSeek-Lite），走独立 Provider 实例，不走完整 AgentRunner
2. 摘要过程使用独立任务队列异步执行，严格不参与主 Runner 循环
3. 摘要请求只传原始对话文本 + 元信息，不注入长期记忆上下文（避免递归）
4. 增加摘要频率限制：每个 session 每天最多 N 次，每次间隔 >= 30 分钟
5. 摘要前做成本收益判断：只有当前上下文 Token 数 > 阈值（如 20K）且预估压缩收益 > 摘要成本时才触发

---

### 5. JSONL Compact 破坏原子性

**问题**:

Spec 说 "JSONL 格式保证写入原子性（追加一行）"。追加一行确实是原子的，但 Session compact（超过 1000 条消息后压缩）需要**重写整个文件**。如果在重写过程中崩溃，整个 Session 历史可能丢失。

**修改建议**:

compact 时必须写入临时文件 + 原子 rename，与 CheckpointManager 的策略一致：

```python
async def compact(self, session_file: Path):
    messages = await self._read_all(session_file)
    compacted = self._summarize_and_trim(messages)
    tmp_file = session_file.with_suffix(".jsonl.tmp")
    await self._write_all(tmp_file, compacted)
    tmp_file.rename(session_file)  # 原子操作
```

---

### 6. AgentRunner 每次迭代都保存 Checkpoint，开销可能过大

**问题**:

Runner 伪代码显示每次迭代（每个 tool_call 往返）都调用 `checkpoint_callback` 保存快照。一次典型的 Agent 运行可能有 5-10 个 tool_call，意味着 5-10 次快照写入。

**修改建议**:

不是每次迭代都保存，而是在以下时机保存：
1. 开始执行第一个工具调用前（初始状态）
2. 每 N 次迭代后（N 默认 3）
3. 执行耗时 > T 秒的工具调用后（T 默认 30s）

这样既保证恢复点足够近，又不会过度 I/O。

---

### 7. 用户确认流程阻塞 Runner

**问题**:

Tool 设计中有 `is_readonly` 标记，"需要用户确认才能写"。但 AgentRunner 的循环是同步的：

```
LLM.chat() -> tool_calls -> ToolRegistry.execute() -> 下一轮 LLM.chat()
```

如果在 `execute()` 中等待用户确认，需要消息往返：Agent -> Channel -> 用户 -> Channel -> Agent。这会长时间阻塞 Runner，超时计时器仍在走。

**修改建议**:

将写操作确认建模为两步流程：

1. 工具执行前检测到需要确认 -> 不执行实际操作，返回特殊结果 `{"requires_user_approval": true, "approval_id": "xxx", "action_summary": "将写入文件 /path/to/file"}`
2. 将确认请求作为 OutboundMessage 发送给用户
3. 用户确认后，以新 InboundMessage 形式回到 Loop
4. Loop 识别为"批准回复"，调用 `ToolRegistry.approve_and_execute(approval_id)`
5. 工具继续执行，结果注入 AgentRunner 的消息列表

这要求 AgentRunner 支持"挂起等待外部输入"的状态——与 Checkpoint 恢复机制配合工作。

---

## P2（影响可维护性/运维能力）

### 8. SubAgent 定义严重不足

**当前状态**: 整个 SubAgent 子系统只有 4 行表格式描述。

**缺失的关键定义**:

- 隔离边界不清：仅上下文隔离？还是独立 ToolRegistry？独立 Session？独立进程？
- "独立 FileStates" 是什么？Spec 未定义该概念
- 31 秒超时数字来源不明，且不可配置
- 子代理结果如何回传给父代理？直接拼入上下文？还是结构化返回（类似 tool_result）？
- 子代理的 Token 消耗如何计入父代理的总预算？
- 如果子代理错误地 spawn 了孙子代理怎么办？

**修改建议**:

如果第一版不需要 SubAgent，直接从 Spec 中移除，避免半成品接口。如果确实需要，需要补充：

```python
@dataclass
class SubAgentSpec:
    prompt: str
    tools: list[str]          # 子代理可用工具白名单
    model: str                # 可选，默认继承
    max_iterations: int = 3
    timeout_seconds: int = 60
    inherit_memory: bool = False  # 是否共享长期记忆
    context_isolation: Literal["full", "tools_only"] = "full"

@dataclass
class SubAgentResult:
    content: str
    tool_calls_count: int
    token_usage: TokenUsage
    error: str | None
```

递归 spawn 策略：默认禁止子代理 spawn 孙子代理（`max_depth=1`）。

---

### 9. 降级矩阵缺乏可操作性

**当前状态**: 5 种故障场景各有 1 句话描述，无法落地。

每个条目需要补充的维度：

| 维度 | 含义 |
|------|------|
| 检测方式 | 如何发现故障（health check / 异常捕获 / 定时扫描） |
| 检测阈值 | 多少次失败/多长时间判定为故障 |
| 降级行为 | 降级后的具体行为，哪些功能受影响 |
| 恢复检测 | 如何判断故障已恢复 |
| 自动恢复 | 是否能自动切回正常模式，还是需要重启 |

以"Disk 满"为例：

```
检测方式:   每次 Checkpoint/Session 写操作后捕获 ENOSPC 错误
检测阈值:   首次 ENOSPC 即触发
降级行为:   拒绝写操作（checkpoint/session/memory 全部只读）
            所有工具写操作返回错误
            通过 MessageBus 向所有活跃 Channel 发送告警消息
恢复检测:   每 60 秒尝试一次 1KB 测试写入
自动恢复:   测试写入成功后自动取消只读模式，无需重启
```

---

### 10. 没有消息去重机制

**问题**: 微信/QQ 频道因网络抖动经常重复投递同一条消息。当前 Spec 没有任何幂等性设计，会导致同一消息被处理多次。

**修改建议**:

1. `InboundMessage` 增加 `message_id` 字段（由 Channel 从平台获取）
2. Loop 中增加消息去重：维护一个 LRU + TTL 的去重缓存（最近 1000 条，TTL 5 分钟）
3. 命中重复消息时直接丢弃，记录 WARN 日志

---

### 11. 没有监控指标定义

**问题**: WatchPanel 有 Rich UI，但没有定义需要采集哪些指标。没有指标，监控面板只是装饰。

**建议的最小指标集**:

| 指标 | 含义 | 采集点 |
|------|------|--------|
| `msg_latency_p50/p99` | 消息处理延迟 | Loop.run 入口/出口 |
| `msg_throughput` | 每分钟处理消息数 | 滚动窗口计数 |
| `token_per_turn` | 每轮对话 Token 消耗 | ContextBuilder + Runner |
| `tool_call_count` | 每种工具的调用次数 | ToolRegistry.execute |
| `error_rate` | 错误率（按类型分） | 异常捕获点 |
| `queue_depth` | MessageBus 各队列深度 | 每次 publish/consume |
| `checkpoint_size` | 检查点文件大小 | CheckpointManager.save |
| `session_count` | 活跃 Session 数 | SessionManager |

---

## P3（可在实现中逐步修正）

### 12. Provider 流式接口设计不够精确

`chat()` 返回 `LLMResponse` 但 streaming 又通过 `AsyncIterator[str]`，这两者矛盾——流式调用时 tool_calls 是增量到达的。

**建议**: Provider 流式接口改为结构化事件流：

```python
from typing import Union
from dataclasses import dataclass

@dataclass
class TextDelta:
    text: str

@dataclass
class ToolCallStart:
    call_id: str
    name: str

@dataclass
class ToolCallDelta:
    call_id: str
    arguments_delta: str  # JSON 增量

@dataclass
class ToolCallEnd:
    call_id: str

@dataclass
class StreamEnd:
    usage: TokenUsage

StreamEvent = Union[TextDelta, ToolCallStart, ToolCallDelta, ToolCallEnd, StreamEnd]
```

---

### 13. Token 计数对多模型适配不足

`tiktoken` 是 OpenAI 的 tokenizer，对 Anthropic Claude 和 DeepSeek 计数偏差可达 20%+。

**建议**: `TokenBudget` 接受 provider 参数，使用对应 tokenizer：
- OpenAI: tiktoken（cl100k_base）
- Anthropic: 使用其官方 token counting endpoint，或近似公式 `chars / 3.5`
- DeepSeek: 近似 `chars / 3` 并上浮 15% 安全边距

---

### 14. Skills XML 摘要的数据来源不明确

Skills 是 `.md` 文件，`build_skills_summary()` 生成 XML。XML 数据从哪来？没有定义解析规则。

**建议**: 规定 `.md` 技能文件的 YAML frontmatter 格式：

```markdown
---
name: code-review
description: 审查代码变更，提供改进建议
always_load: false
dependencies: []
---

# 代码审查技能

## 何时使用
当用户请求代码审查、PR review 或代码质量检查时使用此技能。

## 流程
1. 读取变更文件...
```

`SkillLoader` 解析 frontmatter 生成摘要 XML：

```xml
<skills>
  <skill name="code-review" always_load="false">
    <description>审查代码变更，提供改进建议</description>
  </skill>
</skills>
```

---

### 15. 配置热重载的传播协议缺失

`ConfigLoader.reload()` 支持热重载，但没有定义：
- 哪些模块订阅配置变更
- Provider API Key 变更是否需要重建连接
- Rate Limit 调整能否立即生效
- 配置校验失败时是否回滚

**建议**: 实现简单的观察者模式：

```python
class ConfigReloadHook(Protocol):
    async def on_config_reload(self, new_config: MXWConfig) -> None: ...

# 各模块注册：
config.register_reload_hook(provider_registry.on_config_reload)
config.register_reload_hook(rate_limiter.on_config_reload)
```

---

### 16. 依赖项相关问题

- `tiktoken`: 仅适配 OpenAI，详见问题 13
- `wechaty>=0.10`: Python wechaty SDK 社区维护不稳定，建议准备降级方案（如 itchat 作为 fallback）
- `napcat>=1.0`: NapCat 是 QQ NT Bot 框架，确认生态和 License 兼容性
- `bubblewrap`: Linux only，Windows 下需要替代方案或跳过（标注平台限制）

---

## 总结

| 优先级 | 数量 | 关键问题 |
|--------|------|----------|
| P0 | 3 | 状态机顺序、并发模型、流式通道 |
| P1 | 4 | 记忆递归、Compact 原子性、Checkpoint 频率、用户确认 |
| P2 | 4 | SubAgent、降级矩阵、消息去重、监控指标 |
| P3 | 5 | Provider 流式接口、Token 适配、Skill 解析、热重载、依赖 |

**建议**: 先把 P0 和 P1 的 7 个问题在设计阶段修复，进入开发后可以大幅减少返工。P2 和 P3 可以在迭代中逐步完善。
