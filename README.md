# MXWbot

轻量级多渠道 AI 聊天机器人框架。支持微信/QQ/Email，内置文件操作、Shell 执行、Web 搜索等工具，8 状态事件驱动编排，流式输出，定时任务与智能心跳。

## 快速开始

```bash
# 安装
git clone <repo-url> && cd mxwbot
uv venv && uv pip install -e ".[dev]"

# 初始化配置
mxwbot onboard --wizard

# 交互式对话
mxwbot agent

# 或单次消息
mxwbot agent -m "帮我写个 Python 排序脚本"
```

## CLI 命令

| 命令 | 说明 |
|------|------|
| `mxwbot agent` | 交互式 REPL 对话（支持流式输出） |
| `mxwbot agent -m "msg"` | 单次消息模式 |
| `mxwbot serve` | 启动全系统（channel + cron + heartbeat） |
| `mxwbot onboard` | 初始化配置和工作空间 |
| `mxwbot channel list` | 查看频道状态 |
| `mxwbot cron add "name" "msg" --every 30m` | 添加定时任务 |
| `mxwbot cron list` | 列出定时任务 |
| `mxwbot heartbeat status` | 心跳服务状态 |
| `mxwbot watch` | Rich 实时监控面板 |
| `mxwbot config validate` | 校验配置文件 |
| `mxwbot skill list` | 列出已加载技能 |

## 配置文件

```yaml
# config.yaml
workspace: .mxwbot

providers:
  - name: openai
    api_key: "sk-your-key"
    model: gpt-4
    max_tokens: 4096

channels:
  - type: wechat
    enabled: true
    settings:
      token: "your-wechat-token"

agent:
  max_iterations: 5
  timeout_seconds: 120
```

配置文件查找顺序: `--config` 参数 → `MXWBOT_CONFIG` 环境变量 → `./config.yaml` → `~/.mxwbot/config.yaml`

## 架构

```
Channel (微信/QQ/Email) → Bus → LoopPool → Loop (8 状态状态机)
                                  ├── COMMAND   (斜杠命令检测)
                                  ├── RESTORE   (Checkpoint 恢复)
                                  ├── COMPACT   (Token 压缩)
                                  ├── BUILD     (上下文组装)
                                  ├── RUN       (LLM ReAct 循环)
                                  ├── SAVE      (会话持久化)
                                  ├── RESPOND   (回复投递)
                                  └── DONE

cron/heartbeat → SystemManager.process_direct() → Loop → AgentRunResult
CLI agent → process_direct() → Loop → AgentRunResult
```

## 核心特性

- **8 状态事件驱动编排**: StateManager + 转移表，handler 返回事件字符串
- **流式输出**: `AgentRunner.run_stream()` → `BusStreamHook` → Channel 实时推送
- **多渠道**: 微信 (ilink HTTP 长轮询) / QQ (botpy SDK) / Email (IMAP+SMTP)
- **工具系统**: Read/Write/Edit/Glob/Grep 文件操作, Shell 执行 (sandbox), Web 搜索/抓取, Cron 定时
- **记忆系统**: SQLite + FTS5 长期记忆, LLM 摘要压缩, 重要性评分
- **定时任务**: CronService (at/every/cron 三种调度, JSON 持久化)
- **智能心跳**: HeartbeatService (读 HEARTBEAT.md → LLM 决策 skip/run)
- **确认机制**: 写操作批量确认, FIFO 精准匹配, TTL 超时兜底
- **安全**: Token AES-GCM 加密, SSRF URL 校验, 路径遍历防护, 日志脱敏

## 项目结构

```
mxwbot/
├── cli/          CLI 命令 (agent/serve/onboard/channel/heartbeat/cron/watch/config/skill)
├── core/         核心引擎 (loop, runner, context, state, hook, skill, subagent)
│   └── tools/    工具系统 (filesystem, shell, sandbox, web, cron, mcp)
├── providers/    LLM 供应商 (OpenAI/Anthropic/DeepSeek)
├── channel/      多渠道 (weixin/qq/email)
├── memory/       记忆系统 (SQLite+FTS5, summarizer, token_budget)
├── session/      会话管理 (JSONL 持久化, 去重, asyncio.Lock)
├── checkpoint/   检查点 (策略驱动快照, 原子写入)
├── bus/          消息总线 (Inbound/Outbound/Stream, 确认握手)
├── config/       配置系统 (Pydantic, YAML, ENV override)
├── system/       系统管理 (SystemManager, ManagementAPI)
├── cron/         定时任务 (CronService, CronSchedule)
├── heartbeat/    心跳服务 (HeartbeatService LLM 决策)
├── watch/        监控面板 (Rich TUI, MetricsCollector)
└── utils/        工具函数 (security, text, async_utils, logging)
```

## 运行测试

```bash
# 全部测试
.venv/Scripts/python.exe -m pytest tests/ -v

# 单模块
.venv/Scripts/python.exe -m pytest tests/test_core/test_loop.py -v

# 覆盖率
.venv/Scripts/python.exe -m pytest tests/ --cov=mxwbot --cov-report=term-missing
```

## 依赖

- **Core**: pydantic, typer, rich, httpx, aiohttp, openai, anthropic, tiktoken, aiofiles, aiosqlite, pyyaml, croniter, prompt-toolkit
- **WeChat**: cryptography (token 加密)
- **QQ**: qq-botpy (QQ Bot API SDK)
- **Email**: 纯标准库 (imaplib/smtplib/email)

## 设计哲学

- **无冗余类**: 纯函数优先，只在需要状态时建类
- **异常不穿透**: Provider 异常转 `LLMResponse.error`, callers 不 try/except
- **配置驱动**: 加 channel 只改 yaml 不改代码
- **安全优先**: 路径校验、命令注入检测、SSRF 防护、Token 加密

## License

MIT
