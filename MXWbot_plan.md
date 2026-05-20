# MXWbot 项目开发计划 (Development Plan v1.0)

> **规格依据**: MXWbot_DESIGN_SPEC.md v1.2
> **开发模式**: 分阶段迭代，每阶段产出可独立测试的最小交付物

---

## 一、模块依赖关系图

```
                          ┌─────────────┐
                          │  cli/main   │  Phase 9
                          └──────┬──────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
       ┌────────────┐   ┌────────────┐   ┌─────────────┐
       │ heartbeat/ │   │  watch/    │   │  skills/    │  Phase 8-9
       └─────┬──────┘   └─────┬──────┘   └──────┬──────┘
             │                │                  │
             └────────────────┼──────────────────┘
                              │
                       ┌──────┴──────┐
                       │  core/loop  │  Phase 6
                       └──────┬──────┘
                              │
         ┌────────────┬───────┼───────┬────────────┐
         ▼            ▼       │       ▼            ▼
  ┌──────────┐ ┌──────────┐   │ ┌──────────┐ ┌──────────┐
  │ context  │ │ runner   │   │ │ subagent │ │ channel/ │ Phase 4-7
  └────┬─────┘ └────┬─────┘   │ └────┬─────┘ └────┬─────┘
       │            │         │      │            │
       └────────────┼─────────┘      │            │
                    │                │            │
         ┌──────────┼──────────┐     │            │
         ▼          ▼          ▼     │            │
  ┌──────────┐ ┌──────────┐ ┌───────┴──┐  ┌───────┴──┐
  │  memory/ │ │  tools/  │ │  skill   │  │   bus/    │ Phase 3-5
  └────┬─────┘ └────┬─────┘ └────┬─────┘  └─────┬─────┘
       │            │            │              │
       └────────────┼────────────┘              │
                    │                           │
         ┌──────────┼──────────┐                │
         ▼          ▼          ▼                │
  ┌──────────┐ ┌──────────┐ ┌──────────┐        │
  │providers/│ │ session/ │ │ state.py │  Phase 2-3
  └────┬─────┘ └────┬─────┘ └────┬─────┘        │
       │            │            │              │
       └────────────┼────────────┘              │
                    │                           │
                    ▼                           ▼
              ┌──────────┐              ┌───────────┐
              │ config/  │              │  utils/   │  Phase 1
              └──────────┘              └───────────┘
```

---

## 二、分阶段开发计划

### Phase 1: 项目骨架与基础设施（第 1-2 天）

**目标**: 项目可启动、配置可加载、数据结构可导入

| # | 任务 | 文件 | 产出 | 依赖 |
|---|------|------|------|------|
| 1.1 | 初始化项目骨架 | `pyproject.toml`, `__init__.py` | 可 pip install -e . | 无 |
| 1.2 | 配置 Schema 定义 | `config/schema.py` | 全部 Pydantic 配置模型 | 无 |
| 1.3 | 配置加载器 | `config/loader.py` | YAML/ENV → MXWConfig | 1.2 |
| 1.4 | 路径管理器 | `config/path.py` | 运行时目录自动创建 | 1.2 |
| 1.5 | 安全工具函数 | `utils/security.py` | 路径校验、命令注入检测 | 无 |
| 1.6 | 文本处理工具 | `utils/text.py` | think 清洗、Token 估算、消息裁剪 | 无 |
| 1.7 | 异步工具函数 | `utils/async_utils.py` | timeout 包装器、ConcurrencyLimiter | 无 |
| 1.8 | 结构化日志 | `utils/logging.py` | JSONL 日志 | 无 |
| 1.9 | 消息数据结构 | `bus/messages.py` | InboundMessage / OutboundMessage / StreamDelta | 无 |
| 1.10 | **Phase 1 单元测试** | `tests/` | 配置加载/路径/工具函数通过 | 1.2-1.9 |

**校验标准**: `from mxwbot.config import get_config` 成功，所有 Pydantic 模型通过 schema 校验。

---

### Phase 2: LLM Provider 层（第 3-4 天）

**目标**: 可调用 LLM，供应商可切换，流式/非流式分离

| # | 任务 | 文件 | 产出 | 依赖 |
|---|------|------|------|------|
| 2.1 | Provider 基类与数据结构 | `providers/base.py` | LLMProvider / LLMResponse / LLMStreamChunk / ToolCallDelta / LLMCallPurpose | 无（仅依赖 Python 标准库） |
| 2.2 | Provider 注册中心 | `providers/registry.py` | ProviderRegistry 工厂 | 2.1 |
| 2.3 | OpenAI Provider | `providers/openai_provider.py` | chat() + chat_stream() | 2.1 |
| 2.4 | Anthropic Provider | `providers/anthropic_provider.py` | chat() + chat_stream() | 2.1 |
| 2.5 | DeepSeek Provider | `providers/deepseek_provider.py` | chat() + chat_stream() | 2.1 |
| 2.6 | **Phase 2 集成测试** | `tests/test_providers/` | 各 Provider 基本调用通过 | 2.1-2.5 |

**校验标准**: `provider.chat_stream(messages, tools)` 返回 `StreamDelta` 流，mock 模式下所有 Provider 统一返回格式。

---

### Phase 3: 核心基础设施（第 5-7 天）

**目标**: 状态机、会话、检查点、消息总线、Token 预算就绪

| # | 任务 | 文件 | 产出 | 依赖 |
|---|------|------|------|------|
| 3.1 | 状态机 | `core/state.py` | TurnState(8状态) + StateManager + DegradationPolicy | 无 |
| 3.2 | 消息总线 | `bus/queue.py` | MessageBus（publish_*/subscribe_* + request_confirmation + 流式通道） | 1.9 |
| 3.3 | 会话管理器 | `session/manager.py` | SessionManager（JSONL + asyncio.Lock + 去重缓存 + compact 原子替换） | 1.4 |
| 3.4 | Token 预算 | `memory/token_budget.py` | TokenBudget（count / usage_ratio / truncate） | 2.1（tiktoken） |
| 3.5 | 检查点管理器 | `checkpoint/manager.py` | CheckpointManager（策略驱动保存 + 原子写入 + 恢复） | 1.4 |
| 3.6 | **Phase 3 单元测试** | `tests/test_core/test_state.py` 等 | 状态转移/会话保存恢复/Checkpoint 原子性 | 3.1-3.5 |

**校验标准**:
- StateManager 拒绝非法转移
- Session 并发写后数据完整
- Checkpoint 写入中断不损坏数据（临时文件 + rename）
- 消息总线 publish → subscribe 消息不丢失

---

### Phase 4: 记忆系统（第 8-10 天）

**目标**: 工作记忆 / 情景记忆 / 长期记忆三级体系可用

| # | 任务 | 文件 | 产出 | 依赖 |
|---|------|------|------|------|
| 4.1 | 工作记忆 | `memory/working_memory.py` | WorkingMemory（当前上下文 + 活跃实体） | 无 |
| 4.2 | 长期记忆存储 | `memory/long_term_memory.py` | LongTermMemory（SQLite + FTS5 全文检索） | 1.4 |
| 4.3 | LLM 摘要器 | `memory/summarizer.py` | MemorySummarizer（purpose=SUMMARY 隔离调用） | 2.1 |
| 4.4 | 混合检索 | `memory/retrieval.py` | HybridRetrieval（关键词 + 语义 RRF 融合） | 4.2 |
| 4.5 | 情景记忆 | `memory/episodic_memory.py` | EpisodicMemory（摘要压缩 + 关键信息提取） | 4.1, 4.3, 3.4 |
| 4.6 | 记忆写入 | `memory/update.py` | MemoryUpdater（重要性判断 + 去重 + 写入） | 4.2, 4.3 |
| 4.7 | 记忆统筹 | `memory/core.py` | MemoryManager（统一调度 + 后台合并） | 4.1-4.6 |
| 4.8 | **Phase 4 集成测试** | `tests/test_memory/` | 三级记忆读写 + 检索命中率 | 4.1-4.7 |

**校验标准**:
- 摘要压缩后关键信息不丢失
- FTS5 检索毫秒级返回
- MemorySummarizer 调用 LLM 时不触发记忆检索（purpose 隔离验证）

---

### Phase 5: 工具系统（第 11-13 天）

**目标**: 所有工具注册可用，类型转换 + JSON Schema 验证链完整

| # | 任务 | 文件 | 产出 | 依赖 |
|---|------|------|------|------|
| 5.1 | Tool 基类 | `core/tools/base.py` | Tool(ABC) + JSON_TYPE_MAP + 类型转换链 + Schema 验证 | 无 |
| 5.2 | ToolRegistry | `core/tools/register.py` | 注册/获取/列表/OpenAI Schema 导出 | 5.1 |
| 5.3 | 文件系统工具 | `core/tools/filesystem.py` | Read/Write/Edit/ListDir/Glob/Grep Tool | 5.1, 1.5 |
| 5.4 | Shell 工具 | `core/tools/shell.py` | ShellTool（timeout / env白名单 / pattern黑白名单） | 5.1, 1.5 |
| 5.5 | Sandbox 工具 | `core/tools/sandbox.py` | SandboxTool（bubblewrap 封装） | 5.1, 5.4 |
| 5.6 | Web 工具 | `core/tools/web.py` | WebSearchTool + WebFetchTool（DDG/Tavily/Kagi） | 5.1 |
| 5.7 | Cron 工具 | `core/tools/cron.py` | CronTool（定时提醒和任务调度） | 5.1 |
| 5.8 | MCP 工具 | `core/tools/mcp.py` | McpTool（连接/列举/调用 MCP 服务） | 5.1 |
| 5.9 | **Phase 5 测试** | `tests/test_tools/` | 每个 Tool 的 execute + 验证链 | 5.1-5.8 |

**校验标准**:
- is_readonly 工具可并行执行
- 非只读工具注册到 Registry 后能被 Runner 识别
- 路径穿越检测生效、Shell 注入检测生效

---

### Phase 6: 核心引擎（第 14-17 天）

**目标**: AgentRunner + ContextBuilder + Hook + SkillLoader 全部就绪，可跑完整 ReAct 循环

| # | 任务 | 文件 | 产出 | 依赖 |
|---|------|------|------|------|
| 6.1 | Hook 系统 | `core/hook.py` | AgentHook + StreamProcessHook（think 清洗 + 增量裁剪 + 流式→Bus） | 3.2 |
| 6.2 | SkillLoader | `core/skill.py` | SkillLoader（扫描 + frontmatter 解析 + 懒加载 + 热重载） | 无 |
| 6.3 | 内置技能文件 | `skills/builtin/*.md` | code-review / doc-writer / commit 等技能 | 6.2 |
| 6.4 | ContextBuilder | `core/context.py` | ContextBuilder（按 LLMCallPurpose 分支构建上下文） | 6.2, 4.7, 2.1 |
| 6.5 | AgentRunSpec + AgentRunResult | `core/runner.py` | 配置与结果数据结构 | 2.1, 5.2, 6.1 |
| 6.6 | AgentRunner 核心循环 | `core/runner.py` | LLM + Tool 循环（流式 + 批量确认 + 策略 Checkpoint） | 6.5, 5.2, 3.2, 3.5 |
| 6.7 | SubAgentManager | `core/subagent.py` | SubAgentManager + SubAgentConfig/Result/Event + 隔离 + 超时 | 6.6, 5.2 |
| 6.8 | **Phase 6 集成测试** | `tests/test_core/` | Runner 完整循环 + ContextBuilder 多 purpose 分支 | 6.1-6.7 |

**校验标准**:
- `AgentRunner.run()` 完整执行至少 3 轮 ReAct 循环
- ContextBuilder.build(AGENT) 包含记忆+s技能，build(SUMMARY) 不包含
- 非只读工具触发批量确认，确认拒绝后不执行
- SubAgent 超时自动取消

---

### Phase 7: Channel 多渠道层（第 18-20 天）

**目标**: 微信/QQ/Email 三个频道可收发消息，确认拦截生效

| # | 任务 | 文件 | 产出 | 依赖 |
|---|------|------|------|------|
| 7.1 | BaseChannel 抽象 | `channel/base.py` | send + send_stream + on_message + 确认拦截模式 + TTL 清理 | 3.2 |
| 7.2 | 微信 Channel | `channel/weixin.py` | WeChat 收发（wechaty/itchat 适配） | 7.1 |
| 7.3 | QQ Channel | `channel/qq.py` | QQ 收发（go-cqhttp/napcat 适配） | 7.1 |
| 7.4 | Email Channel | `channel/email.py` | IMAP 轮询 + SMTP 发送 | 7.1 |
| 7.5 | **Phase 7 测试** | `tests/test_channel/` | Mock 频道收发 + 确认拦截流程 | 7.1-7.4 |

**校验标准**:
- 任意 Channel 收到的消息 → Bus → LoopPool → 处理后 → Channel 发送回用户
- 确认请求 → 用户回复 Y/N → 正确拦截并转换为 confirmation_response
- TTL 超时后用户过期回复不被误拦截

---

### Phase 8: 编排器组装（第 21-22 天）

**目标**: LoopPool 消费循环启动，全链路跑通

| # | 任务 | 文件 | 产出 | 依赖 |
|---|------|------|------|------|
| 8.1 | Loop 编排器 | `core/loop.py` | Loop（6 组件编排 + 状态机驱动 + 消息预处理 + 命令分发） | 6.4, 6.6, 6.7, 4.7, 3.3, 3.1 |
| 8.2 | LoopPool 并发管理 | `core/loop.py` | LoopPool（Semaphore(20) + session locks + 消费循环 + confirmation_response 分发） | 8.1, 3.2 |
| 8.3 | **Phase 8 集成测试** | 端到端测试 | 全链路：消息入站 → 去重 → 编排 → 流式 → 回复 | 8.1-8.2 |

**校验标准**:
- 单用户连续 3 条消息，顺序处理不越序
- 2 个不同 chat_id 并发消息，同时处理互不阻塞
- 同一 Session 并发消息串行化

---

### Phase 9: 基础设施服务（第 23-24 天）

**目标**: 监控面板 + 心跳引擎 + CLI 可操作

| # | 任务 | 文件 | 产出 | 依赖 |
|---|------|------|------|------|
| 9.1 | 监控指标定义 | `watch/metrics.py` | SystemMetrics / AgentMetrics / ChannelMetrics / DegradationEvents | 无 |
| 9.2 | Rich 监控面板 | `watch/panel.py` | WatchPanel（TUI 面板 + 颜色区分 + 实时刷新） | 9.1 |
| 9.3 | 心跳存储 | `heartbeat/storage.py` | TaskStorage（SQLite 持久化） | 无 |
| 9.4 | 心跳调度器 | `heartbeat/scheduler.py` | TaskScheduler（threading.Thread + run_coroutine_threadsafe + 复用 publish_inbound） | 9.3, 3.2 |
| 9.5 | HeartbeatEngine | `heartbeat/__init__.py` | HeartbeatEngine（组合 Scheduler + Storage） | 9.3, 9.4 |
| 9.6 | CLI 入口 | `cli/main.py` | `mxwbot serve` / `config` / `watch` / `skill list` | 8.2, 7.1, 9.2, 9.5 |
| 9.7 | **Phase 9 测试** | 端到端 | CLI 启动 → 全服务就绪 → 消息处理 → 监控可见 | 9.1-9.6 |

**校验标准**:
- `mxwbot serve` 启动后所有服务就绪
- `mxwbot watch` 面板实时显示各指标
- 定时任务到期后自动执行并通过 Loop 处理

---

### Phase 10: 测试完善与文档（第 25-27 天）

**目标**: 覆盖率达标、边界 case 覆盖、文档齐全

| # | 任务 | 产出 | 依赖 |
|---|------|------|------|
| 10.1 | 测试覆盖率补齐 | 整体覆盖率 > 80% | Phase 1-9 |
| 10.2 | 降级场景测试 | 6 种故障场景的降级行为验证 | 3.1, Phase 1-9 |
| 10.3 | 并发压力测试 | 20 session 并发 + 100 条消息压测 | 6.6, 8.2 |
| 10.4 | 安全审计 | 路径穿越/命令注入/ID 枚举/敏感信息泄露 | Phase 1-9 |
| 10.5 | API 文档生成 | 所有公共 API 有 docstring | Phase 1-9 |
| 10.6 | README + 快速开始 | 用户文档 | Phase 1-9 |

---

## 三、时间线总览

```
Week 1 (Day 1-5):   ██████ Phase 1 ───█ Phase 2 ────────
Week 2 (Day 6-10):  ── Phase 2 ───██████ Phase 3 ────────
Week 3 (Day 11-15): ── Phase 3 ───██████ Phase 4 ───────█ Phase 5 ──
Week 4 (Day 16-20): ── Phase 5 ───██████ Phase 6 ─────────████ Phase 7
Week 5 (Day 21-25): ─██ Phase 7 ──████ Phase 8 ──████ Phase 9 ──────
Week 6 (Day 26-27): ──── Phase 9 ───██████ Phase 10 ────────────────
```

| Phase | 名称 | 工作日 | 核心里程碑 | 状态 |
|-------|------|--------|-----------|------|
| 1 | 项目骨架与基础设施 | 1-2 | `from mxwbot.config import get_config` | ✅ 已完成 |
| 2 | LLM Provider 层 | 3-4 | 三厂商 chat/chat_stream 统一返回 | ✅ 已完成 |
| 3 | 核心基础设施 | 5-7 | 状态机 + 会话 + Checkpoint + Bus 就绪 | ✅ 已完成 |
| 4 | 记忆系统 | 8-10 | 三级记忆可读写检索 | ✅ 已完成 |
| 5 | 工具系统 | 11-13 | 8 种工具全部注册可执行 | ✅ 已完成 |
| 6 | 核心引擎 | 14-17 | ReAct 完整循环 + SubAgent | ✅ 已完成 |
| 7 | Channel 多渠道层 | 18-20 | 微信/QQ/Email 互通 | ⬜ 待开始 |
| 8 | 编排器组装 | 21-22 | 全链路跑通 | ⬜ 待开始 |
| 9 | 基础设施服务 | 23-24 | CLI + 监控 + 心跳 | ⬜ 待开始 |
| 10 | 测试完善与文档 | 25-27 | 覆盖率 > 80% | ⬜ 待开始 |

> 状态图例: ⬜ 待开始 | 🔄 进行中 | ✅ 已完成

---

## 四、可并行化建议

以下模块组之间无依赖，可由多人并行开发：

| 并行组 | 包含模块 | 建议开始时间 |
|--------|----------|-------------|
| A 组 | Phase 4 记忆系统 与 Phase 5 工具系统 | Phase 3 完成后 |
| B 组 | Phase 5.3-5.8 各工具实现 | Tool 基类完成后即可并行 |
| C 组 | Phase 7 各 Channel 实现 | BaseChannel 完成后即可并行 |

---

## 五、风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| Provider API 变更 | Phase 2/6 阻塞 | Provider 层专注接口抽象，具体实现可延迟适配 |
| Channel SDK 不稳定 | Phase 7 延迟 | 优先完成 Email（最稳定），微信/QQ 可后补 |
| Memory FTS5 性能 | Phase 4 检索慢 | 预留向量检索降级路径，先用关键词保证可用 |
| Tool 安全漏洞 | Phase 5/10 返工 | Phase 5 每个 Tool 写完后立即做安全审计，不堆积到 Phase 10 |
| 确认流程多轮交互导致死锁 | Phase 6/7 | Bus.request_confirmation 强制 60s 超时 + TTL 双重兜底 |

---

## 六、阶段完成工作流

### 6.1 阶段状态更新

每个 Phase 开始时，开发者将本 plan 文档中对应 Phase 的状态从 `⬜ 待开始` 改为 `🔄 进行中`。
完成后从 `🔄 进行中` 改为 `✅ 已完成`。

### 6.2 阶段完成文档生成

每个 Phase 完成后，**必须生成** `docs/phaseX_completed.md`（X 为阶段号，如 `phase1_completed.md`），放置在项目 `docs/` 目录下。

**模板如下**:

```markdown
# Phase X 完成报告

> **完成日期**: YYYY-MM-DD
> **实际耗时**: N 天
> **对应计划**: MXWbot_plan.md Phase X

---

## 一、本阶段目标

{从 plan 文档复制本阶段的"目标"描述}

---

## 二、完成的项目结构

```
mxwbot/
├── {本阶段新增/修改的目录树，只展示本阶段涉及的文件}
```

---

## 三、完成的模块与文件清单

| 文件 | 行数 | 核心类/函数 | 说明 |
|------|------|-------------|------|
| `xxx.py` | N | `ClassName` | 一句话说明 |

---

## 四、本阶段核心设计要点

{2-4 个关键设计决策，为什么要这样做}

---

## 五、校验标准达成情况

- [ ] 校验项 1 — {通过/未通过}
- [ ] 校验项 2 — {通过/未通过}

---

## 六、已知遗留问题

| 问题 | 严重程度 | 计划解决阶段 |
|------|----------|-------------|
| xxx | P2 | Phase Y |

---

## 七、下阶段准备

- 下阶段: Phase {X+1} — {名称}
- 需要本阶段产出的: {列出下阶段依赖的本阶段产物}
```

### 6.3 文件存放约定

```
mxwbot/
├── docs/
│   ├── phase1_completed.md
│   ├── phase2_completed.md
│   └── ...
├── MXWbot_DESIGN_SPEC.md
└── MXWbot_plan.md
```

---

## 七、每日检查点

每 Phase 进行中时每日检查：

- [ ] 今日任务产出文件可 import
- [ ] 增量测试通过（不破坏已有测试）
- [ ] 未闭合的 TODO 记入 backlog

---

> **版本**: v1.2 | **日期**: 2026-05-18 | **配套文档**: MXWbot_DESIGN_SPEC.md v1.3
