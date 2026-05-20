# 新项目规格SPEC
基于nanobot思想的个人agent助手：
## 项目需求
- 实现类似于claw项目的多渠道沟通
- 实现类似于nanobot项目的轻量代码
- 实现类似于hermes agent的记忆系统
- 基于harness Engineering理念引入state状态机制


- 持久化设计

## 需要遵循的设计原则
- 开闭
- 单一职责
- 迪米特法则
- 接口隔离原则
- 依赖倒转原则
**核心设计原则**是**保持可维护 可二次开发 不需要重新修改核心代码 代码可读性一定要强 要足够安全** 所有包和机制请考虑安全设计 不过不要太过冗余的编码 
## 配置config包
- 设置配置系统
- 设计要求：完成系统配置有关的路径设定和核心代码所涉及的配置设定
```python
参考目录
loader.py--Configuration loading utilities
path.py--基于当前运行配置，提供统一的运行时路径管理 具体所需目录根据项目设定来设计 如果有cron等方法就能够获取或者创建特定子目录
schema.py--基于pydantic来实现具体的config定义：
    - 基类
    - toolsConfig
    - Channelconfig
    - AgentDeafultConfig
    - ProviderConfig
    - HeartBeatConfig
    - GatewayConfig
    - WebSearchConfig
    - WebFEtchConfig
    - WebToolsConfig
    - ExecToolConfig
    - Mcp有关的配置
    - 其余的请你看情况设计
```
## watch层
实现监控面板 Rich 终端 UI，颜色/面板区分事件类型 针对loop中的核心事件进行审计
## providers包
- LLM provider抽象层
- 基类定义基本的LLM供应商类：ToolCallRequest(A tool call request from the LLM.)+LLMResponse+LLMProvider等
- 扩展要求针对主流LLM厂商进行定义
## skills包 
- 按照claude code定义的skill格式 存放内置skill技能
- 暂时还没定义 你可以根据当前项目文件设计
## Session
- 会话管理 JSONL+合法边界保护
## 心跳机制heartbeat层
后台独立进程，自动执行定时任务
任务持久化存储，重启不丢失
## cli层
 Cli命令行基于typer
- 定义命令行入口
## bus
- 设计思路：定义输入输出消息队列  Async message queue for decoupled channel-agent communication.
## channel层
- 设计思路：聊天频道实现
- 基类文件方便后续扩展
- 具体实现文件：暂时考虑weixin+qq+email三个渠道的实现
## 监控层
- 
## 核心组件Core包：
- 请注意：当下所有文件只负责构件类和方法 不涉及到真正的实现 除开测试 具体调用面向用户层在cli包 
### 1.loop：核心的中央编排器：
#### 六大组件
- contextBuilder(由context.py实现)
- SubagentManger
- MemoryConsolidator(由memory.py实现)
- 工具管理
- AgentRunner(由runner实现)
- sessionManager(会话管理) 会话考虑异步锁
- 
#### 默认支持流式输出 
- 要配套流式输出的处理 这一步由hook实现
     - 1.拼接历史增量和新文本prev_clean+delta 
     - 2.清洗增量文本（去掉think标签）得到new_clean 
    -  3.从new_clean中切出真正的增量incremental（去掉prev_clean部分） 
     - 4.如果incremental非空且_on_stream回调存在，则调用_on_stream(incremental)将增量文本发送出去 
- 支持命令分发 如果是斜杠命令 
- 文本输入输出要做预处理 以增加对于token的节省 
核心执行模式基于 ReAct（Reasoning + Acting），但做了三层增强
传统 ReAct:
  Thought → Action → Observation → Thought → Action → ...

Enhanced ReAct:
  ┌─────────────────────────────────────────────┐
  │ Layer 1: Plan (for complex tasks)            │
  │   用户意图 → 分解为子任务 → 依赖排序          │
  │   仅当任务复杂度超过阈值时触发（阈值判断）                │
  ├─────────────────────────────────────────────┤
  │ Layer 2: Enhanced ReAct Loop                 │
  │   Context → Reason → Act → Observe → Reflect │
  │   每步有预算控制 + 超时 + 错误处理            │
  ├─────────────────────────────────────────────┤
  │ Layer 3: Metacognition (事后反思)            │
  │   结果验证 → 自我评估 → 记忆更新             │
  │   "我做对了吗？学到了什么？"                  │
  └─────────────────────────────────────────────┘
#### 核心流程
1. 系统消息检测 → 特殊路径（有限历史）
2. 记录输入消息
3. 获取/创建 Session（按 channel:chat_id）
4. 斜杠命令分发（/clear、/stop 等）
5. Token 预算记忆合并（在上下文构建前释放空间） 
6. 上下文构建（系统提示词 + 历史 + 用户消息）
7. AgentRunner.run() —— LLM+工具循环 具体循环实现由runner实现
8. 保存本轮消息到 Session
9. 调度后台记忆合并
### 2. state.py
- 引入状态机 根据Hassaness engine的思想设计状态机 把一轮对话分为多个状态 定义状态锚点 方便监控
```
#状态参考 可以根据实际项目需求更改
class TurnState(Enum):
    RESTORE = auto()
    COMPACT = auto()
    COMMAND = auto()
    BUILD = auto()
    RUN = auto()
    SAVE = auto()
    RESPOND = auto()
    DONE = auto()
```
当出现组件故障时，设计降级原则以保证程序不会崩溃
├── MCP Server 断开     → 该 MCP 工具不可用，其余正常
├── Memory DB 连接失败  → 仅使用 Session 历史 + 本地文件记忆
├── Channel 断开        → 该频道消息暂存，其余频道正常
├── Rate Limit 触发     → 自动降低处理速度
└── Disk 满             → 拒绝写操作，只读 + 告警
```
设计原则
1. Never Crash Silently    — 任何异常都必须产生可追溯的记录
2. Always Recoverable      — 从任何状态都能恢复到已知合法状态
3. Degrade with Dignity    — 非核心功能故障时降级而非崩溃
4. Budget All Resources    — CPU/内存/Token/时间 都有硬上限
5. Trust via Verification  — 关键输出必须经过验证
6. Time-bound Everything   — 每个操作都有超时，不设上限即为 bug
```
### 3.context:
- 系统提示词+历史消息+用户消息+工具描述+渐近线披露skills(节省token)
     - Identity:不同平台的使用准则 主要针对Windows和unix 通过提示词工程嵌入 工作区路径 记忆文件位置 行为准则规定（提示词）
     - skills summary:一个xml格式的技能总结，包含技能名称、描述、路径和可用性等信息。
     - long-term Memory
     - 总是要求满足的技能全文:少量 如人格skills

### 4.tool包
- base.py：
     - 定义全局变量JSON_TYPE_MAP方便类型判断
     - 基于ABC定义工具基类Tool
     - 设计原则:
         - 定义工具规范 抽象方法如name、description、parameters(JSON Schema for tool parameters.)、执行方法等等
         - 定义工具边界 安全规范 如判断工具是不是只读的 工具是否可以并行执行
         - 定义统一父类工具函数:实现完整的类型转换和验证链：1.类型转换 2、JSON Schema验证等
- register.py：主要负责进行多个tool工具的注册、管理、运行 当一个loop执行时 默认定义一个ToolRegister() 必备工具就注册进去 其余工具则按序注册（实际调用时判断是否需要）
- 暂时考量所需要的工具
     - filesystem.py:统一构件文件系统工具 负责write、read、edit、listdir等工具的实现 同样是一基类+多具体实现类 方便后续扩展
     - web.py：负责网络搜索+web_fetch tool基类+多子类实现续扩展 通过配置找到具体供应商 默认供应商为DuckDuckGo 支持Tavily + Kagi的适配
     - cron.py：Cron tool for scheduling reminders and tasks
     - shell.py：
     - mcp.py：兼容mcp服务工具
     - sandbox工具:基于bubblewrap 安全地执行LLM提供的 Shell 命令
     - 等等 还有什么工具需求自己补足
### 5.subagent：agent子代理功能
子agent管理机制
- 主要负责后台执行任务
定义子代理管理类参考SubAgentmanager
- 子代理隔离上下文 不共享session
- 最大全局并发执行数5
- 设计子代理状态类 方便记录子代理执行日志
```python
#主要功能核心代码参考
        try:
            # Build subagent tools (no message tool, no spawn tool)
            tools = ToolRegistry()
            allowed_dir = self.workspace if (self.restrict_to_workspace or self.exec_config.sandbox) else None
            extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
            # Subagent gets its own FileStates so its read-dedup cache is
            # isolated from the parent loop's sessions (issue #3571).
            from nanobot.agent.tools.file_state import FileStates
            file_states = FileStates()
            tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=extra_read, file_states=file_states))
            tools.register(WriteFileTool(workspace=self.workspace, allowed_dir=allowed_dir, file_states=file_states))
            tools.register(EditFileTool(workspace=self.workspace, allowed_dir=allowed_dir, file_states=file_states))
            tools.register(ListDirTool(workspace=self.workspace, allowed_dir=allowed_dir, file_states=file_states))
            tools.register(GlobTool(workspace=self.workspace, allowed_dir=allowed_dir, file_states=file_states))
            tools.register(GrepTool(workspace=self.workspace, allowed_dir=allowed_dir, file_states=file_states))
            if self.exec_config.enable:
                tools.register(ExecTool(
                    working_dir=str(self.workspace),
                    timeout=self.exec_config.timeout,
                    restrict_to_workspace=self.restrict_to_workspace,
                    sandbox=self.exec_config.sandbox,
                    path_append=self.exec_config.path_append,
                    allowed_env_keys=self.exec_config.allowed_env_keys,
                    allow_patterns=self.exec_config.allow_patterns,
                    deny_patterns=self.exec_config.deny_patterns,
                ))
            if self.web_config.enable:
                tools.register(
                    WebSearchTool(
                        config=self.web_config.search,
                        proxy=self.web_config.proxy,
                        user_agent=self.web_config.user_agent,
                    )
                )
                tools.register(
                    WebFetchTool(
                        config=self.web_config.fetch,
                        proxy=self.web_config.proxy,
                        user_agent=self.web_config.user_agent,
                    )
                )
            system_prompt = self._build_subagent_prompt()
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            result = await self.runner.run(AgentRunSpec(
                initial_messages=messages,
                tools=tools,
                model=self.model,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                hook=_SubagentHook(task_id, status),
                max_iterations_message="Task completed but no final response was generated.",
                error_message=None,
                fail_on_tool_error=True,
                checkpoint_callback=_on_checkpoint,
            ))
```
### 6.runner:解耦编排和纯引擎
- 实现LLM调用->工具执行->再调用的循环 具体LLM的调用由provider包提供不同供应商的调用该方法
- 实现与业务逻辑无关的LLM+工具执行循环
- 实际设计应该以一下三个类的形式为参考
     - AgentRunspec(Configuration for a single agent execution.) 
     - AgentRunner(Run a tool-capable LLM loop without product-layer concerns.)
     - AgentRunResult(Outcome of a shared agent execution.)
- 保护机制:最大迭代次数(默认为3)+超时
### 8.hook 钩子系统 在一系列动作前进行预处理
#### 设计原则
- 针对一系列动作前进行硬编码检查
#### 参考代码如下
```python
#参考设计
class AgentHook:
    """Minimal lifecycle surface for shared runner customization."""

    def __init__(self, reraise: bool = False) -> None:
        self._reraise = reraise

    def wants_streaming(self) -> bool:
        return False

    async def before_iteration(self, context: AgentHookContext) -> None:
        pass

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        pass

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        pass

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        pass

    async def after_iteration(self, context: AgentHookContext) -> None:
        pass

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        return content
```

### 9 skill.py
#### 设计原则
- 支持可插拔的skill
- 支持按需加载
#### 类文档
- 设计skillLoader类 
     - 功能需求：
     1.实现自动扫描工作区目录实现动态加载 
     2.实现加载xml格式skill列表 
     3.实现通过name加载skill具体内容 
     4.支持热更新 
     5.实现懒加载 首次调用格式才加载完整内容
     6.通过name获取skill描述
     7.获取skill 元数据犯法
     8.检查当前环境十分满足某个技能所声明的依赖需求
- 兼容claude code技能

## memory设计
- 参考hermes agent的设计理念  持续学习用户偏好
![Img](./FILES/新项目规格SPEC.md/img-20260511171140.png)
可参考架构目录
memory/
├── __init__.py                 # 对外暴露主要接口：MemoryManager
├── core.py                     # 核心类：MemoryManager，负责统筹工作记忆、触发摘要、调度长期记忆
├── working_memory.py           # 工作记忆：当前会话上下文、会话状态
├── episodic_memory.py          # 情景记忆：摘要生成、关键信息提取、Token控制
├── long_term_memory.py         # 长期记忆：SQLite存储 + FTS5全文检索 + 向量检索
├── retrieval.py                # 检索模块：混合检索（关键词+语义）接口
├── update.py                   # 写入模块：重要性判断、结构化写入
├── summarizer.py               # LLM摘要与关键信息提取器
├── token_budget.py             # Token计数器与预算管理                
## utils包
- 定义其他包需要的辅助函数 按照具体代码开发来定义
## 还要添加的功能
- 本目录下的功能暂时不知道如何设计 请你在不改动前面目录的情况下 遵循设计原则 添加相应代码文件规格
请你帮我想想还有那些 简洁的设计 可以帮我1.节省token 2.任务完成更好的设计 