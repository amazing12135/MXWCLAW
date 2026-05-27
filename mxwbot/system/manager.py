"""SystemManager — global component registry & lifecycle manager.

All CLI subcommands interact with running components through this
single entry point.  ``serve`` mode wire everything together; admin
subcommands talk to a running serve process via the ManagementAPI.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from mxwbot.bus.messages import InboundMessage
from mxwbot.bus.queue import MessageBus
from mxwbot.checkpoint.manager import CheckpointManager
from mxwbot.config.schema import MXWConfig
from mxwbot.core.context import ContextBuilder
from mxwbot.core.loop import LoopPool
from mxwbot.core.runner import AgentRunner, AgentRunSpec
from mxwbot.core.skill import SkillLoader
from mxwbot.core.tools.register import ToolRegistry
from mxwbot.cron.cron_service import CronService, CronJob
from mxwbot.cron.types import CronSchedule
from mxwbot.heartbeat.heartbeat_service import HeartbeatService
from mxwbot.memory.core import MemoryManager
from mxwbot.providers.base import LLMProvider
from mxwbot.session.manager import SessionManager

logger = logging.getLogger("mxwbot.system.manager")


# ---------------------------------------------------------------------------
# Component state model
# ---------------------------------------------------------------------------

class ComponentState(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class Component:
    name: str
    state: ComponentState
    instance: Any
    start_method: str = "start"
    stop_method: str = "stop"
    depends_on: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SystemManager
# ---------------------------------------------------------------------------

class SystemManager:
    """Single-owner registry of all system components.

    CLI subcommands (``serve``, ``channel start``, ``heartbeat run`` …)
    talk to the running serve process through HTTP.  This class is
    instantiated inside the serve process and holds the canonical
    component graph.
    """

    def __init__(self, config: MXWConfig) -> None:
        self.config = config
        self._components: dict[str, Component] = {}
        self._running = False

        # -- shared infrastructure (always created) --------------------------
        ws = config.workspace
        self.bus = MessageBus()
        self.sessions = SessionManager(ws)
        self.checkpoint = CheckpointManager(ws)
        self.skills = SkillLoader(ws / "skills")

        # -- lazily initialised in bootstrap() --------------------------------
        self.provider: LLMProvider | None = None
        self.memory: MemoryManager | None = None
        self.tools: ToolRegistry | None = None
        self.context_builder: ContextBuilder | None = None
        self.runner: AgentRunner | None = None
        self.loop_pool: LoopPool | None = None
        self.cron_service: CronService | None = None
        self.heartbeat: HeartbeatService | None = None

    # ------------------------------------------------------------------
    # Bootstrap & serve
    # ------------------------------------------------------------------

    async def bootstrap(self) -> None:
        """Async init: create Provider → Memory DB → Tools → register components."""
        from mxwbot.providers.registry import ProviderRegistry

        # Provider
        registry = ProviderRegistry.from_configs(self.config.providers)
        self.provider = registry.get_default()

        # Memory
        self.memory = MemoryManager(self.config.workspace, self.provider)
        await self.memory.init_db()

        # Context builder
        self.context_builder = ContextBuilder(self.config.workspace)

        # Tools
        from mxwbot.core.tools.filesystem import (
            EditFileTool, GlobTool, GrepTool, ListDirTool, ReadFileTool, WriteFileTool,
        )
        from mxwbot.core.tools.web import WebFetchTool, WebSearchTool
        from mxwbot.core.tools.shell import ShellTool
        from mxwbot.core.tools.cron import CronTool
        ws_path = self.config.workspace
        tool_cfg = self.config.tools
        unrestricted = tool_cfg.unrestricted_filesystem
        self.tools = ToolRegistry()
        # File tools — with optional unrestricted filesystem access
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool, GlobTool, GrepTool):
            self.tools.register(cls(ws_path, unrestricted=unrestricted))
        # Shell tool — with configurable sandbox backend
        self.tools.register(ShellTool(
            ws_path,
            timeout=tool_cfg.shell.timeout_seconds,
            sandbox_backend=tool_cfg.shell.sandbox_backend,
        ))
        self.tools.register(CronTool(ws_path))
        self.tools.register(WebSearchTool())
        self.tools.register(WebFetchTool())
        # Meta-tool for lazy-loading extension tool definitions
        from mxwbot.core.tools.base import get_tool_schema_instance
        self.tools.register(get_tool_schema_instance(self.tools))

        # Runner
        self.runner = AgentRunner()

        # LoopPool (core processing engine — shared by all paths)
        agent_cfg = self.config.agent
        self.loop_pool = LoopPool(
            bus=self.bus,
            sessions=self.sessions,
            memory=self.memory,
            context_builder=self.context_builder,
            runner=self.runner,
            provider=self.provider,
            tools=self.tools,
            checkpoint=self.checkpoint,
            skills=self.skills,
            max_concurrent=agent_cfg.max_concurrent_sessions,
            checkpoint_interval=agent_cfg.checkpoint_interval,
            max_iterations=agent_cfg.max_iterations,
            skip_confirmation=agent_cfg.skip_confirmation,
        )

        # Register core component
        self._register(Component("loop_pool", ComponentState.STOPPED, self.loop_pool))

    async def serve(self) -> None:
        """Start all components: channels, cron, heartbeat + outbound consumers."""
        self._running = True

        # Create and register channels
        for ch_cfg in self.config.channels:
            if not ch_cfg.enabled:
                continue
            name = f"{ch_cfg.type}_channel"
            instance = self._create_channel(ch_cfg)
            self._register(Component(name, ComponentState.STOPPED, instance,
                depends_on=["loop_pool"]))

        # Create and register cron service
        cron_path = self.config.workspace / "cron" / "jobs.json"
        self.cron_service = CronService(
            store_path=cron_path,
            on_job=self._on_cron_job,
        )
        self._register(Component("cron", ComponentState.STOPPED, self.cron_service,
            depends_on=["loop_pool"]))

        # Create and register heartbeat
        hb_cfg = self.config.heartbeat
        self.heartbeat = HeartbeatService(
            workspace=self.config.workspace,
            provider=self.provider,
            enabled=hb_cfg.enabled,
            on_execute=self._on_heartbeat_execute,
            on_notify=self._on_heartbeat_notify,
        )
        self._register(Component("heartbeat", ComponentState.STOPPED, self.heartbeat,
            depends_on=["loop_pool"]))

        # Start all in dependency order
        order = self._topological_order()
        for comp in order:
            logger.info("Starting %s …", comp.name)
            await self.start_component(comp.name)

        # Start channel outbound consumers
        self._outbound_tasks: list[asyncio.Task] = []
        for comp in self.list_components():
            if "channel" in comp.name and comp.state == ComponentState.RUNNING:
                ch = comp.instance
                t = asyncio.create_task(self._channel_outbound_loop(ch))
                self._outbound_tasks.append(t)
                logger.info("Channel %s outbound consumer started", comp.name)

        logger.info("All components started — consuming messages")
        await self.loop_pool.start()

    async def _channel_outbound_loop(self, channel: Any) -> None:
        """Read OutboundMessage from the bus queue and send via channel."""
        queue = await self.bus.subscribe(channel.name)
        while self._running:
            try:
                msg = await queue.get()
                await channel.send(msg)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Channel send failed for %s", channel.name)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Gracefully stop all running components (reverse order)."""
        self._running = False
        # Cancel channel outbound consumers first
        for t in getattr(self, "_outbound_tasks", []):
            t.cancel()
        for comp in reversed(self._topological_order()):
            if comp.state == ComponentState.RUNNING:
                await self.stop_component(comp.name)

    # ------------------------------------------------------------------
    # Component lifecycle
    # ------------------------------------------------------------------

    async def start_component(self, name: str) -> bool:
        comp = self._components.get(name)
        if comp is None:
            logger.error("Unknown component: %s", name)
            return False
        if comp.state == ComponentState.RUNNING:
            return True

        # Start dependencies first
        for dep in comp.depends_on:
            if not await self.start_component(dep):
                logger.error("Cannot start %s: dependency %s failed", name, dep)
                return False

        comp.state = ComponentState.STARTING
        try:
            inst = comp.instance
            method = getattr(inst, comp.start_method)
            if asyncio.iscoroutinefunction(method):
                await method()
            else:
                method()
            comp.state = ComponentState.RUNNING
            logger.info("Component %s → RUNNING", name)
            return True
        except Exception as exc:
            comp.state = ComponentState.ERROR
            logger.error("Failed to start %s: %s", name, exc)
            return False

    async def stop_component(self, name: str) -> bool:
        comp = self._components.get(name)
        if comp is None:
            return False
        if comp.state != ComponentState.RUNNING:
            return True

        comp.state = ComponentState.STOPPING
        try:
            method = getattr(comp.instance, comp.stop_method)
            if asyncio.iscoroutinefunction(method):
                await method()
            else:
                method()
            comp.state = ComponentState.STOPPED
            logger.info("Component %s → STOPPED", name)
            return True
        except Exception as exc:
            comp.state = ComponentState.ERROR
            logger.error("Failed to stop %s: %s", name, exc)
            return False

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def is_running(self, name: str) -> bool:
        comp = self._components.get(name)
        return comp is not None and comp.state == ComponentState.RUNNING

    def list_components(self) -> list[Component]:
        return list(self._components.values())

    def component_status(self, name: str) -> ComponentState:
        comp = self._components.get(name)
        return comp.state if comp else ComponentState.STOPPED

    # ------------------------------------------------------------------
    # Cron callback — publish to Bus
    # ------------------------------------------------------------------

    async def _on_cron_job(self, job: CronJob) -> str | None:
        """Execute a cron job via LoopPool, returning agent response."""
        result = await self.loop_pool.process_direct(
            job.payload.message,
            f"cron:{job.id}",
            channel=job.payload.channel or "system",
            chat_id=job.payload.to or "cron",
        )
        return result.content if result else None

    # ------------------------------------------------------------------
    # Heartbeat callbacks
    # ------------------------------------------------------------------

    async def _on_heartbeat_execute(self, tasks: str) -> str | None:
        """Execute heartbeat task via LoopPool (silent, keep 20 msgs)."""
        result = await self.loop_pool.process_direct(
            tasks,
            "heartbeat",
            channel="heartbeat",
            chat_id="system",
            keep_recent=20,
        )
        return result.content if result else None

    async def _on_heartbeat_notify(self, response: str) -> None:
        """Deliver heartbeat response to the best available channel."""
        for comp in self.list_components():
            if "channel" in comp.name and comp.state == ComponentState.RUNNING:
                ch = comp.instance
                if hasattr(ch, "_send_text"):
                    channel_type = comp.name.replace("_channel", "")
                    for key in list(getattr(self.sessions, "_sessions", {}).keys()):
                        if key.startswith(channel_type):
                            session = self.sessions._sessions.get(key)
                            if session:
                                await ch._send_text(session.chat_id, response)
                                return
        logger.info("Heartbeat response (no active channel): %.200s", response)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _register(self, comp: Component) -> None:
        self._components[comp.name] = comp

    def _topological_order(self) -> list[Component]:
        """Return components in dependency order (leaves first)."""
        visited: set[str] = set()
        ordered: list[Component] = []

        def visit(name: str) -> None:
            if name in visited:
                return
            visited.add(name)
            comp = self._components.get(name)
            if comp:
                for dep in comp.depends_on:
                    visit(dep)
                ordered.append(comp)

        for name in self._components:
            visit(name)
        return ordered

    def _create_channel(self, ch_cfg: Any) -> Any:
        settings = ch_cfg.settings or {}
        if ch_cfg.type == "wechat":
            from mxwbot.channel.weixin import WeChatChannel, WeixinConfig
            cfg = WeixinConfig(
                token=settings.get("token", ""),
                base_url=settings.get("base_url", "https://ilinkai.weixin.qq.com"),
                state_dir=str(self.config.workspace / "weixin"),
            )
            return WeChatChannel(self.bus, config=cfg)
        if ch_cfg.type == "qq":
            from mxwbot.channel.qq import QQChannel, QQConfig
            cfg = QQConfig(
                app_id=settings.get("app_id", ""),
                secret=settings.get("secret", ""),
            )
            return QQChannel(self.bus, config=cfg)
        if ch_cfg.type == "email":
            from mxwbot.channel.email import EmailChannel, EmailConfig
            cfg = EmailConfig(
                imap_host=settings.get("imap_host", ""),
                imap_port=settings.get("imap_port", 993),
                imap_username=settings.get("imap_username", ""),
                imap_password=settings.get("imap_password", ""),
                smtp_host=settings.get("smtp_host", ""),
                smtp_port=settings.get("smtp_port", 587),
                smtp_username=settings.get("smtp_username", ""),
                smtp_password=settings.get("smtp_password", ""),
                from_address=settings.get("from_address", ""),
            )
            return EmailChannel(self.bus, config=cfg)
        raise ValueError(f"Unknown channel type: {ch_cfg.type}")
