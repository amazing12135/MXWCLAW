# MXWbot 第二版设计审计结果

> **审计日期**: 2026-05-11
> **审计对象**: MXWbot_DESIGN_SPEC.md v1.1
> **审计视角**: 资深 Agent 系统开发者
> **基于**: 第一版审计结果，逐项核验修复情况

---

## 一、v1 审计修复核验

| v1 编号 | v1 问题 | v2 修复方案 | 状态 |
|---------|---------|------------|------|
| P0-1 | 状态机顺序不合理 | COMMAND 前置 + COMPACT 守卫条件（usage_ratio > 0.8 AND msg_count > 20） | ✅ 通过 |
| P0-2 | 并发模型完全未定义 | LoopPool + asyncio.Semaphore(20) + 按 session_key 隔离串行 | ✅ 通过 |
| P0-3 | 流式输出到 Channel 路径缺失 | StreamDelta + MessageBus.publish_stream_delta() + Channel.send_stream() | ✅ 通过 |
| P1-4 | 记忆系统递归依赖 | LLMCallPurpose 三元枚举（AGENT/SUMMARY/SYSTEM），ContextBuilder 按 purpose 分支，SUMMARY 走最小上下文 | ✅ 通过 |
| P1-5 | JSONL Compact 破坏原子性 | 未修复——仍只说 "行级原子，不原地修改"，compact 重写文件的安全性未提及 | ❌ 遗留 |
| P1-6 | Checkpoint 每次迭代写入 | 策略驱动保存：工具调用前 + 周期兜底（每 N 次迭代）+ 异常紧急，三种时机 | ✅ 通过 |
| P1-7 | 用户确认流程阻塞 Runner | 未修复——is_readonly 仍只有标记，无确认流程设计 | ❌ 遗留 |
| P2-8 | SubAgent 定义严重不足 | 完整 SubAgentConfig / SubAgentResult / SubAgentEvent 数据结构 + 隔离清单 + 错误处理 | ✅ 通过 |
| P2-9 | 降级矩阵缺乏可操作性 | 部分修复——新增 "Runner 异常" 行，但所有条目仍只有一句话，缺检测方式/阈值/恢复检测 | ❌ 遗留 |
| P2-10 | 没有消息去重机制 | idempotency_key + SessionManager 去重缓存 | ✅ 通过 |
| P2-11 | 没有监控指标定义 | metrics.py 四类结构化指标（SystemMetrics / AgentMetrics / ChannelMetrics / DegradationEvents） | ✅ 通过 |
| P3-12 | Provider 流式接口不精确 | 未修复——仍同时写 "chat() 返回 LLMResponse" 和 "streaming 通过 AsyncIterator[str]" | ❌ 遗留 |
| P3-13 | Token 计数多模型适配 | 未修复——仍只有 tiktoken | ❌ 遗留（降为低优先级） |
| P3-14 | Skills XML 来源不明 | 未修复——SkillLoader 仍只说 "生成 XML 格式技能摘要"，未定义 .md → XML 的解析规则 | ❌ 遗留 |
| P3-15 | 配置热重载传播协议 | 未修复 | ❌ 遗留（降为低优先级） |
| P3-16 | 依赖项相关问题 | 未修复（相同依赖列表） | ❌ 遗留（降为低优先级） |

**核验结论**: 8 项已修复通过，8 项遗留（其中 3 项为 P1 级别，需要关注）。

---

## 二、v2 新增问题

### 问题 A：Runner 代码与流程图 Checkpoint 逻辑互相矛盾

**严重程度**: P1（实现阶段必引发 bug）

**位置**:
- 3.3.4 runner.py 伪代码
- 2.2 Mermaid 流程图

**矛盾详情**:

Python 伪代码使用 `elif`——周期保存只在**没有 tool_calls 时**才触发：

```python
if response.tool_calls:
    await spec.checkpoint_callback(...)       # 工具调用前保存
    results = await execute_tools(...)         # 执行工具
elif iteration > 0 and iteration % spec.checkpoint_interval == 0:
    await spec.checkpoint_callback(...)       # 纯文本周期兜底
```

但 Mermaid 流程图中周期保存是**工具执行之后**触发的：

```
K3["ToolRegistry.execute()"] --> K4{"iter % N == 0?"}
K4 -->|是| K4a["CheckpointManager.save() 周期兜底"]
```

两处逻辑矛盾：代码说工具调用和周期保存互斥，流程图说周期保存发生在工具调用之后。

**修复建议**:

以代码为准（`elif` 更合理——工具调用前已经保存过了，无需周期兜底重复保存），修改流程图：

```mermaid
K2 -->|是| K2a["CheckpointManager.save() 工具调用前"] --> K3["ToolRegistry.execute()"] --> K1
K2 -->|否| K5["返回最终内容"]
K2 -->|文本AND iter%N==0| K4a["CheckpointManager.save() 周期兜底"]
```

或者更简洁：周期保存分支从工具执行路径中移除，放在纯文本路径上。

---

### 问题 B：流式异常中断时缺少 Channel 通知

**严重程度**: P1（影响用户体验——用户会看到半截消息永久悬挂）

**位置**: 2.2 Mermaid 流程图 + 3.3.4 runner.py

**问题**:

LLM 流式输出中途崩溃时（如流式传输中 Provider 连接断开），Channel 已通过 `send_stream()` 推送了部分文本。当前异常处理只做了 Checkpoint 保存：

```
K --> K_ERR["异常捕获"]
K_ERR --> K_ERR_SAVE["CheckpointManager.save() 紧急保存"]
```

Channel 不知道流已中断，用户看到半截消息永远等不到结尾。

**修复建议**:

1. MessageBus 增加错误通知方法：

```python
async def publish_stream_error(self, stream_id: str, error: str) -> None: ...
```

2. Channel 基类增加对应方法：

```python
async def send_stream_error(self, chat_id: str, error: str) -> None: ...
```

3. Runner 的 except 块中，在保存 Checkpoint 前先发送流错误：

```python
except Exception as e:
    if current_stream_id:
        await bus.publish_stream_error(current_stream_id, str(e))
    if spec.checkpoint_callback:
        await spec.checkpoint_callback(..., emergency=True)
    raise
```

---

### 问题 C：LoopPool 与 MessageBus 消费循环归属模糊

**严重程度**: P2（架构层面模块职责不清）

**位置**: 3.3.1 loop.py + 3.7 bus/queue.py + 第五章启动流程

**问题**:

启动流程中写 "消息到达 → Bus.publish_inbound → Bus.consume_loop() → LoopPool.dispatch()"。但 MessageBus 是消息管道，不应该拥有消费循环逻辑（取消息、去重、分发）。消\
费循环属于编排层的职责。

**修复建议**:

明确归属——LoopPool 拥有消费循环，Bus 只提供队列：

```python
class LoopPool:
    def __init__(self, bus: MessageBus, session_manager: SessionManager):
        self._bus = bus
        self._session_manager = session_manager
        self._semaphore = asyncio.Semaphore(20)
        self._locks: dict[str, asyncio.Lock] = {}

    async def run_forever(self):
        """主消费循环，归 LoopPool 所有"""
        while True:
            msg = await self._bus.input_queue.get()
            if self._session_manager.is_duplicate(msg):
                continue
            asyncio.create_task(self._process(msg))

    async def _process(self, msg: InboundMessage):
        session_key = f"{msg.channel}:{msg.chat_id}"
        async with self._semaphore:
            lock = self._locks.setdefault(session_key, asyncio.Lock())
            async with lock:
                loop = Loop(...)
                await loop.run(msg)
```

从 Bus 中移除 `consume_loop()` 方法。

---

### 问题 D：OutboundMessage.is_streaming 是死字段

**严重程度**: P3（代码整洁）

**位置**: 3.7 bus/messages.py

**问题**:

`OutboundMessage` 有 `is_streaming: bool = False`，但流式输出走 `StreamDelta` + `publish_stream_delta()`，不走 `OutboundMessage`。这个字段永远是 `False`。

**修复建议**:

删除 `is_streaming`。如果需要在流结束后发送一个聚合的完整消息用于审计/日志，可以保留但改名为 `was_streamed: bool`，语义更准确。

---

### 问题 E：SubAgent "独立 Session 不写 JSONL" 用途不明

**严重程度**: P3（文档清晰度）

**位置**: 3.3.7 subagent.py 隔离清单

**问题**:

隔离清单写 "独立 Session（不共享父 Session，不写入 JSONL）"。但 Session 的核心职责就是 JSONL 持久化。不写 JSONL 的 Session 在 SubAgent 生命周期中的作用是什么？仅作内存消息累积器？

**修复建议**:

要么明确这是纯内存的临时消息列表（不需要 SessionManager 参与），直接说 SubAgent 内部维护 `list[Message]`；要么保留 Session 但说明它仅用于 Runner 循环中的消息累积，生命周期在 SubAgent 结束时销毁。

---

### 问题 F：Rate Limiter 组件缺失

**严重程度**: P2（降级矩阵引用了不存在的组件）

**位置**: 2.4 降级矩阵 + 3.9 heartbeat/

**问题**:

降级矩阵两次提到 "Rate Limit 触发 → 自动降低处理速度，滑动窗口动态调整"，但目录结构和所有模块说明中没有任何 Rate Limiter 组件。Provider 内部可能有各自的重试逻辑，但系统的全局 Rate Limiter 不存在。

**修复建议**:

二选一：
- 在 `utils/` 下新增 `rate_limiter.py`（基于滑动窗口的异步速率限制器），降级矩阵引用它
- 明确声明 Rate Limit 由各 Provider 内置处理（如 OpenAI SDK 的 `max_retries`），降级矩阵改为引用 Provider 能力

---

### 问题 G：FileStates 概念多次引用但从未定义

**严重程度**: P3（文档完整性）

**位置**: 3.3.7 subagent.py 隔离清单

**问题**:

SubAgent 隔离清单写 "独立 FileStates（写缓存隔离）"，但整个 Spec 没有任何地方定义 FileStates。它似乎是 tools/filesystem.py 的内部实现细节。

**修复建议**:

如果 FileStates 是内部实现，从隔离清单中移除（外部不感知）。如果它确实是一个需要暴露的概念，在 `core/tools/` 下增加定义。

---

### 问题 H：Heartbeat "独立进程" 仍然含糊

**严重程度**: P2（会影响实现选型）

**位置**: 3.10 heartbeat/scheduler.py

**问题**:

仍然写 "独立线程/进程"。两者的架构含义完全不同：
- 独立进程无法共享 asyncio MessageBus 队列，需要 IPC 方案
- 独立线程可以用 `asyncio.run_coroutine_threadsafe` 向主事件循环投递消息

**修复建议**:

明确为独立线程（`threading.Thread`），通过 `asyncio.run_coroutine_threadsafe` 向 MessageBus 投递定时触发的消息。避免引入不必要的进程间通信复杂度。

如需真正进程级隔离（防止 GIL 影响定时精度），需要额外设计 IPC 通道（如 multiprocessing.Queue 或 Redis pub/sub），但 v1 不需要。

---

## 三、v1 遗留问题详解

### 遗留-1：用户确认流程缺失（原 P1-7）

安全清单中仍有 "工具只读标记 (is_readonly)，需要用户确认才能写"，但整个系统没有用户确认的流程设计。这不仅是文档缺失——没有这个机制，写保护形同虚设。

**建议方案**（已在对话中讨论并确认）：

1. **批量确认**：Runner 每次迭代中，将所有写工具合并为一次确认请求（而非逐工具确认），避免串行等待
2. **超时拒绝**：`asyncio.Event` + `asyncio.wait_for(timeout=30s)`，超时视为拒绝
3. **Bus 确认通道**：MessageBus 增加 `request_confirmation()` / `resolve_confirmation()` 方法
4. **Channel 降级**：
   - 支持交互的频道（如有按钮能力）：渲染为确认卡片
   - 纯文本频道：渲染为 "请回复 Y/N"，临时拦截用户下一条消息做解析

---

### 遗留-2：JSONL Compact 原子性（原 P1-5）

compact 操作仍没有明确标注使用临时文件 + 原子 rename 策略。需要在 SessionManager 文档中补充：

```python
async def compact(self, session_file: Path):
    messages = await self._read_all(session_file)
    compacted = self._summarize_and_trim(messages)
    tmp_file = session_file.with_suffix(".jsonl.tmp")
    await self._write_all(tmp_file, compacted)
    tmp_file.rename(session_file)  # 原子替换
```

---

### 遗留-3：Provider 流式接口描述矛盾（原 P3-12）

3.2 节同时声明 `chat() 返回 LLMResponse` 和 `streaming 通过 AsyncIterator[str]`。这两者在流式调用中矛盾。

**修复建议**：显式拆分为两个方法：

```python
class LLMProvider(ABC):
    # 非流式
    async def chat(self, messages, tools) -> LLMResponse: ...

    # 流式
    async def chat_stream(self, messages, tools) -> AsyncIterator[StreamEvent]: ...
```

---

### 遗留-4：Skills XML 摘要数据来源（原 P3-14）

SkillLoader 描述了 "生成 XML 摘要"，但没有说数据从哪里来。

**修复建议**：在 3.3.6 skill.py 中明确 frontmatter 规则：

```markdown
---
name: code-review
description: 审查代码变更，提供改进建议
always_load: false
---
# 技能内容...
```

SkillLoader 解析 YAML frontmatter 生成 XML 摘要。

---

### 遗留-5~8（低优先级）

| 编号 | 问题 | 建议 |
|------|------|------|
| 遗留-5 | Token 计数多模型适配（原 P3-13） | tiktoken 仅适配 OpenAI，Anthropic/DeepSeek 需要近似估算。可在实现时解决 |
| 遗留-6 | 配置热重载传播协议（原 P3-15） | v1 可暂不实现热重载，启动时一次加载即可 |
| 遗留-7 | 依赖项风险（原 P3-16） | wechaty 社区维护不稳定，bubblewrap 仅 Linux。标注为可选依赖即可 |
| 遗留-8 | 降级矩阵可操作性（原 P2-9） | 每个条目补充检测方式、阈值、恢复检测。可随实现逐步细化 |

---

## 四、总结

### 整体评价

v2 Spec 在架构层面已经扎实——P0 级阻塞问题全部解决，并发模型、流式通道、记忆隔离、SubAgent 定义、消息去重、监控指标均有落地设计。剩余问题集中在：

- **两处逻辑矛盾**（问题 A、C）——流程图 vs 代码、消费循环归属
- **一处功能缺口**（问题 B）——流式异常没有通知 Channel
- **四处文档/接口债务**（问题 D、E、F、G、H）——死字段、未定义概念、含糊表述
- **四处 v1 遗留问题**（遗留 1-4）——确认流程、Compact 原子性、Provider 接口、Skills 解析

### 优先级

| 优先级 | 问题 | 理由 |
|--------|------|------|
| **P1** | 问题 A：Runner Checkpoint 流程图矛盾 | 实现阶段必然出现分歧 |
| **P1** | 问题 B：流式异常无通知 | 用户体验直接影响 |
| **P1** | 遗留-1：用户确认流程 | 写安全虚设 |
| **P2** | 问题 C：消费循环归属 | 架构清晰度 |
| **P2** | 问题 F：Rate Limiter 组件 | 降级矩阵引用不存在组件 |
| **P2** | 问题 H：Heartbeat 进程模型 | 实现选型会受影响 |
| **P2** | 遗留-2：Compact 原子性 | 数据安全 |
| **P3** | 其余 6 项 | 文档/细节，实现中可逐步修正 |

**建议**: P1 三项在进入开发前修正，P2 四项在开发初期解决，P3 随迭代完善。
