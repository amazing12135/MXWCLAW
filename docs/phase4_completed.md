# Phase 4 完成报告

> **完成日期**: 2026-05-13
> **实际耗时**: 1 天
> **对应计划**: MXWbot_plan.md Phase 4

---

## 一、本阶段目标

三级记忆体系：短期记忆（Session）+ 长期记忆（SQLite + FTS5）+ LLM 摘要压缩。

---

## 二、完成的项目结构

```
mxwbot/memory/
├── __init__.py              # 包入口
├── core.py                  # MemoryManager — 统一调度
├── long_term_memory.py      # SQLite + FTS5 + context_for_query()
├── summarizer.py            # LLM 摘要器 (extract_facts + summarise_session)
├── update.py                # 重要性过滤 + 去重 + 写入
└── token_budget.py          # 纯函数 token 计数/裁剪 (Phase 3)

mxwbot/session/
└── manager.py               # Session + SessionManager (重构)

mxwbot/utils/
└── text.py                  # +clean_assistant_replay_text()
```

---

## 三、删除的冗余模块

| 模块 | 去处 |
|------|------|
| `WorkingMemory` | `active_task` → `Session.active_task`；`entities` → `LongTermMemory` 检索；`topics` → LLM 自己看历史 |
| `EpisodicMemory` | 逻辑内联到 `MemoryManager.consolidate()` |
| `MemoryRetrieval` | `context_for_query()` 直接放在 `LongTermMemory` 上 |

---

## 四、记忆数据流

```
═══════════════════════════════════════════════════════════════════════════════
                        LOOP.run() — 每轮对话
═══════════════════════════════════════════════════════════════════════════════

[1. RESTORE] 加载 Session
─────────────────────────────────────────────────────────────────────────────
SessionManager.get_session(channel, chat_id)
    │
    ├─→ 内存缓存命中? → 直接返回 Session
    │
    └─→ 缓存未命中:
          → _load_jsonl("sessions/wechat_u1.jsonl")
          → 成功: Session(messages=[...], consolidated_count=N)
          → 失败: _repair_jsonl() 逐行恢复


[2. COMMAND] 斜杠命令检测
─────────────────────────────────────────────────────────────────────────────
如果消息是 /clear /stop /status:
  → 直接执行，不加载 LLM 上下文
  如果 /clear: session.messages = []
  如果 /stop: 进入关闭流程 (见下)


[3. COMPACT] Token 预算检查（守卫）
─────────────────────────────────────────────────────────────────────────────
count_tokens(session.messages) / max_tokens → usage_ratio

如果 usage_ratio > 0.8 AND msg_count > 20:
  → truncate_messages_smart(messages, max_tokens)
     → token 裁剪 → user-turn 对齐 → 去孤儿 tool


[4. BUILD] 组装 LLM 上下文
─────────────────────────────────────────────────────────────────────────────
┌─ System Prompt ──────────────────────────────────────────────────────┐
│ 你是 MXWbot...                                                       │
│                                                                      │
│ [MemoryManager.get_context(user_query)]  ← 长期记忆检索注入           │
│   回: LongTermMemory.context_for_query("dark mode")                  │
│   回: FTS5 MATCH → "相关记忆:\n- [preference] 用户偏好 dark mode"     │
│                                                                      │
│ [session.active_task] ← 当前任务 (如果有)                             │
│                                                                      │
│ [session.session_summary] ← 已压缩历史摘要 (如果有)                   │
└──────────────────────────────────────────────────────────────────────┘

┌─ History (session.get_history()) ────────────────────────────────────┐
│ session.messages[consolidated_count:]                                │
│   → user turn 对齐                                                   │
│   → 去孤儿 tool result                                               │
│   → assistant 文本清洗 (clean_assistant_replay_text)                  │
│   → media 占位符合成                                                 │
│   → token 裁剪 + 再对齐                                              │
│                                                                      │
│ 结果: [{"role":"user","content":"我喜欢dark mode"},                   │
│         {"role":"assistant","content":"已记录"}]                      │
└──────────────────────────────────────────────────────────────────────┘


[5. RUN] LLM 推理 + 工具执行
─────────────────────────────────────────────────────────────────────────────
AgentRunner.run() 循环...
    → LLM 输出 → content/text 或 tool_calls


[6. SAVE] 持久化 + 记忆合并
─────────────────────────────────────────────────────────────────────────────
短期记忆写入:
  session.append_message({"role": "assistant", "content": resp.content})
    → aiofiles 追加 JSONL
    → session.messages.append()

长期记忆合并:
  MemoryManager.consolidate(session.messages, session.consolidated_count)
    │
    ├─→ [条件触发] usage_ratio > 0.8 AND msg > 20
    │     → messages[consolidated_count:] 前半段
    │     → summarizer.summarise_session()  (LLM 压缩)
    │     → 返回摘要文本 → 存入 session.session_summary
    │     → consolidated_count += split
    │
    └─→ [每轮都做] 最近消息
          → summarizer.extract_facts()
          → 重要性过滤 (importance >= 0.4)
          → 去重 (词重叠 Jaccard > 0.7)
          → LongTermMemory.add_batch()
          → INSERT INTO memories + FTS5 索引自动同步


[7. SAVE - 文件容量保护]
─────────────────────────────────────────────────────────────────────────────
如果 session.message_count > 1000:
  → compact(session, keep_last=200)
     → 保留尾部 200 条 → user-turn 对齐 → 去孤儿 tool
     → 原子重写 (tmp + os.replace)
     → consolidated_count 指针前移


[8. RESPOND] 发送回复
─────────────────────────────────────────────────────────────────────────────
Bus.publish_outbound(OutboundMessage(...))


═══════════════════════════════════════════════════════════════════════════════
                        会话关闭流程
═══════════════════════════════════════════════════════════════════════════════

触发: 用户主动退出 或 Heartbeat 检测不活跃 (待 Phase 9 实现)

MemoryManager.force_consolidate(session.messages, session.consolidated_count)
  → 跳过安全门，强制压缩所有未压缩消息
  → 提取长期事实写入 LTM

SessionManager.close(session)
  → session.clear()          # messages=[], task=None, count=0
  → 从 _sessions 缓存移除
  → 从 _locks / _dedup 移除
  → JSONL 文件保留在磁盘


═══════════════════════════════════════════════════════════════════════════════
                        存储介质对照
═══════════════════════════════════════════════════════════════════════════════

  短期记忆                              长期记忆
  ────────                              ────────
  位置: Session.messages (内存)         位置: SQLite memories 表 (磁盘)
  文件: sessions/*.jsonl               文件: memory/memory.db
  格式: [{"role":"user","content":"..."}]格式: {id, content, category, importance}
  生命周期: 会话期间                    生命周期: 永久（跨会话）
  容量: 1000 条 → auto compact          容量: 无上限
  注入: get_history() 管道               注入: context_for_query() → FTS5 MATCH
  写入: append_message()                写入: add_batch()  (由 MemoryUpdater 调)
```

---

## 五、校验标准达成情况

- [x] LongTermMemory 写入 → FTS5 检索命中 — 通过
- [x] MemorySummarizer LLM 提取结构化 fact — 通过
- [x] MemoryManager.get_context() 返回格式化记忆文本 — 通过
- [x] consolidate() 条件触发 + 返回摘要 — 通过
- [x] Session.get_history() 完整管道 — 通过
- [x] 紧凑存储：WorkingMemory/Episodic/Retrieval 删除 — 通过
- [x] 227 测试全部通过 — 通过

## 六、已知遗留问题

| 问题 | 严重程度 | 计划解决阶段 |
|------|----------|-------------|
| `truncate_messages_smart` fallback 搜索是简单线性扫描 | P3 | Phase 10 |
| `_is_duplicate` 词重叠法对短句效果一般 | P3 | Phase 10 (可换 embedding cosine) |

## 七、下阶段准备

- 下阶段: Phase 5 — 工具系统
- 需要本阶段产出的: `session/manager.py` (历史管理), `memory/core.py` (长期存储)
- 状态: ✅ 就绪
