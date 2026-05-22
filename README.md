# MXWbot

轻量级多渠道 AI 聊天机器人框架。支持微信/QQ/Email，内置文件操作、Shell 执行、Web 搜索等工具，8 状态事件驱动编排，流式输出，定时任务与智能心跳。

---

## 快速开始

```powershell
# 1. 安装
uv venv && uv pip install -e ".[dev]"

# 2. 初始化配置（交互式向导，选择 provider + 输入 API key）
mxwbot onboard --wizard

# 3. 交互式对话（流式输出）
mxwbot agent

# 4. 单次消息
mxwbot agent -m "帮我写个 Python 排序脚本"
```

---

## 完整命令参考

### `mxwbot agent` — AI 对话

```powershell
# 交互式 REPL（流式逐 token 显示，支持 prompt_toolkit 历史/粘贴）
mxwbot agent

# 单次消息模式
mxwbot agent -m "读一下 config.yaml 的内容"

# 指定配置 + 详细日志
mxwbot agent --config prod.yaml --verbose
```

### `mxwbot onboard` — 初始化

```powershell
# 快速生成默认 config.yaml
mxwbot onboard

# 交互式向导（选 provider、输 API key）
mxwbot onboard --wizard

# 指定工作空间
mxwbot onboard --workspace ./my-bot --wizard
```

### `mxwbot serve` — 启动全系统

启动后同时运行：channel 消息监听、cron 定时任务、heartbeat 心跳、管理 API。

```powershell
# 启动
mxwbot serve --config config.yaml

# 启动 + 开启监控面板
mxwbot serve --config config.yaml --watch

# 详细日志
mxwbot serve --config config.yaml --verbose
```

### `mxwbot channel` — 频道管理

```powershell
# 列出所有频道状态
mxwbot channel list

# 微信扫码登录（无需预先配置 token）
mxwbot channel login wechat

# 重新扫码（token 过期时）
mxwbot channel login wechat --force

# 在 serve 运行时单独启动/停止频道
mxwbot channel start wechat
mxwbot channel stop wechat
```

### `mxwbot cron` — 定时任务

支持三种调度方式：一次性（`--at`）、间隔重复（`--every`）、crontab 表达式（`--cron`）。

```powershell
# 添加一个每 30 分钟执行的任务
mxwbot cron add "检查邮件" "查看是否有新邮件并总结" --every 30m

# 每天早上 9 点执行
mxwbot cron add "日报" "生成今日工作总结" --cron "0 9 * * *" --tz "Asia/Shanghai"

# 一次性定时（30 分钟后提醒）
mxwbot cron add "提醒" "该休息了" --every 30m --once

# 查看所有任务
mxwbot cron list

# 查看任务详情
mxwbot cron show <job_id>

# 手动触发任务
mxwbot cron run <job_id>

# 暂停/恢复任务
mxwbot cron disable <job_id>
mxwbot cron enable <job_id>

# 删除任务
mxwbot cron remove <job_id>

# 服务状态
mxwbot cron status
```

### `mxwbot heartbeat` — 智能心跳

读 `HEARTBEAT.md` 文件，用 LLM 决策是否有活跃任务，自动执行。

```powershell
# 启动/停止心跳服务
mxwbot heartbeat start
mxwbot heartbeat stop

# 手动触发一次心跳
mxwbot heartbeat run

# 查看心跳状态
mxwbot heartbeat status
```

### 其他命令

```powershell
# Rich 实时监控面板
mxwbot watch

# 校验配置文件
mxwbot config validate --config config.yaml

# 显示配置（敏感信息脱敏）
mxwbot config show

# 列出已加载的技能
mxwbot skill list
```

---

## 配置文件

`config.yaml` 查找顺序：命令行 `--config` → `MXWBOT_CONFIG` 环境变量 → `./config.yaml` → `~/.mxwbot/config.yaml`

```yaml
workspace: .mxwbot
log_level: INFO

# LLM 供应商（可配置多个）
providers:
  - name: openai
    api_key: "sk-your-key"
    model: gpt-4
    max_tokens: 4096
    temperature: 0.7

  - name: deepseek
    api_key: "sk-your-key"
    model: deepseek-chat
    base_url: https://api.deepseek.com

  - name: anthropic
    api_key: "sk-ant-your-key"
    model: claude-sonnet-4-6

# 频道配置
channels:
  # 微信个人号（ilink HTTP 长轮询，扫码登录）
  - type: wechat
    enabled: true

  # QQ（botpy SDK）
  - type: qq
    enabled: false
    settings:
      app_id: "your-app-id"
      secret: "your-secret"

  # 邮件（IMAP 收 + SMTP 发）
  - type: email
    enabled: false
    settings:
      imap_host: imap.gmail.com
      imap_port: 993
      imap_username: you@gmail.com
      imap_password: "app-password"
      smtp_host: smtp.gmail.com
      smtp_port: 587
      smtp_username: you@gmail.com
      smtp_password: "app-password"
      from_address: you@gmail.com

# Agent 参数
agent:
  max_iterations: 5          # ReAct 循环最大轮数
  timeout_seconds: 120       # 单次处理超时
  checkpoint_interval: 2     # 检查点保存间隔

# 工具
tools:
  filesystem_enabled: true   # 文件读写（限制在 workspace 内）
  shell:
    enabled: true            # Shell 命令执行
    timeout_seconds: 30
```

---

## 定时任务详解

### 三种调度模式

| 模式 | 参数 | 示例 | 说明 |
|------|------|------|------|
| 一次性 | `--at` | `--at "2026-06-01T09:00:00"` | 在指定时间执行一次 |
| 间隔 | `--every` | `--every 5m` / `--every 1h` / `--every 1d` | 每隔 N 秒/分/时/天 |
| Crontab | `--cron` | `--cron "0 9 * * *" --tz "Asia/Shanghai"` | 标准 cron 表达式 |

### 任务自动执行流程

```
CronService 到期 → on_job → publish_inbound("system")
  → LoopPool → Loop → AgentRunner → 执行工具 → 回复
```

### 通过对话创建定时任务

在 `mxwbot agent` 对话中可以直接让 AI 创建：

```
You: 每天早上 9 点帮我检查是否有新邮件
AI:  → 调用 cron 工具 → 创建任务 → "已设置每天 9:00 的邮件检查"
```

### 心跳机制

在 `workspace/HEARTBEAT.md` 中编写你的长期任务清单：

```markdown
# Heartbeat Tasks
- 每天早上 9 点检查是否有新邮件需要回复
- 每周五下午 5 点生成项目周报
- 监控 GitHub 仓库是否有新 PR
```

HeartbeatService 每隔 30 分钟读取此文件，用 LLM 判断是否有任务需要执行。如果有 → 自动触发 agent 处理。

---

## 微信接入

```powershell
# 1. 扫码登录（零配置，不需要 app_id）
mxwbot channel login wechat

# 2. 终端显示 QR URL → 浏览器打开 → 微信扫码 → 确认

# 3. Token 自动 AES-GCM 加密保存到 weixin_state/account.json

# 4. 在 config.yaml 中启用微信
# channels:
#   - type: wechat
#     enabled: true

# 5. 启动
mxwbot serve --config config.yaml

# 6. 用微信给 bot 发消息 → 自动回复
```

---

## 架构

```
Channel (微信/QQ/Email) → Bus → LoopPool → Loop (8 状态状态机)
                                  ├── COMMAND   (斜杠命令 /clear /stop /status /admin)
                                  ├── RESTORE   (Checkpoint 恢复)
                                  ├── COMPACT   (Token 压缩，usage>80% 触发)
                                  ├── BUILD     (上下文组装：身份+技能+记忆+历史)
                                  ├── RUN       (LLM ReAct：思考→调工具→观察，流式输出)
                                  ├── SAVE      (会话 JSONL 持久化)
                                  ├── RESPOND   (回复 + 关流 StreamDelta(is_end=True))
                                  └── DONE

cron/heartbeat → SystemManager.process_direct() → Loop → AgentRunResult
CLI agent → process_direct() → Loop → AgentRunResult (skip_confirmation=True)
```

## 核心特性

- **8 状态事件驱动编排**: handler 返回 "ok"/"error"/"shortcut"/"dispatch"，转移表决定路由
- **流式输出**: `AgentRunner.run_stream()` → `BusStreamHook` → 终端/Channel 实时推送
- **多渠道**: 微信 (ilink HTTP 长轮询 + 扫码登录) / QQ (botpy SDK WebSocket) / Email (IMAP+SMTP)
- **工具系统**: Read/Write/Edit/Glob/Grep 文件, Shell 执行, Web 搜索/抓取, Cron 定时
- **记忆系统**: SQLite+FTS5 长期记忆, LLM 摘要压缩, 重要性评分去重
- **定时任务**: CronService (at/every/cron, timer sleep 到点, JSON 持久化, mtime 自动 reload)
- **智能心跳**: HeartbeatService (读 HEARTBEAT.md → LLM 通过 heartbeat tool 决策 skip/run)
- **确认机制**: 写操作批量确认, FIFO list 精准匹配, TTL Task 管理
- **安全**: Token AES-GCM 加密, SSRF URL 校验 + redirect 跟踪, 路径遍历防护, 日志脱敏, IMAP SSL 校验
- **系统管理**: ManagementAPI (aiohttp, 127.0.0.1:9090), SystemManager 拓扑排序启停

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

| 类别 | 包 |
|------|-----|
| Core | pydantic, typer, rich, httpx, aiohttp, openai, anthropic, tiktoken, aiofiles, aiosqlite, pyyaml, croniter, prompt-toolkit |
| WeChat | cryptography (token 加密, optional) |
| QQ | qq-botpy (optional) |
| Email | 纯标准库 |

## 设计哲学

- **无冗余类**: 纯函数优先，只在需要状态时建类
- **异常不穿透**: Provider 异常转 `LLMResponse.error`，caller 不 try/except
- **配置驱动**: 加 channel 只改 yaml 不改代码
- **安全优先**: 路径校验、命令注入检测、SSRF 防护、Token 加密

## License

MIT
