"""Pydantic v2 configuration models for MXWbot.

All configuration is nested under the single MXWConfig root model.
Sensitive fields use SecretStr for automatic serialization masking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


# ---------------------------------------------------------------------------
# Gateway / Server
# ---------------------------------------------------------------------------

class GatewayConfig(BaseModel):
    """HTTP gateway / API server configuration."""

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class ProviderConfig(BaseModel):
    """Single LLM provider configuration."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Provider identifier, e.g. 'openai', 'anthropic', 'deepseek'")
    api_key: SecretStr = Field(default=SecretStr(""), description="API key")
    base_url: str | None = Field(default=None, description="Custom base URL for OpenAI-compatible APIs")
    model: str = Field(default="", description="Default model name")
    max_tokens: int = Field(default=4096, ge=1, description="Default max output tokens per call")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------

class ChannelConfig(BaseModel):
    """Configuration for a single channel.

    New channel types can be added by simply adding an entry to the
    ``channels`` list in YAML — no code change required.

    YAML example::

        channels:
          - type: wechat
            enabled: true
            settings:
              adapter: itchat
          - type: discord
            enabled: true
            settings:
              token: "xxx"
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(description="Channel type, e.g. 'wechat', 'qq', 'email', 'discord'")
    enabled: bool = False
    settings: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary channel-specific parameters validated by the adapter",
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class WebSearchConfig(BaseModel):
    """Web search tool configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    provider: Literal["duckduckgo", "tavily", "kagi"] = "duckduckgo"
    api_key: SecretStr = SecretStr("")


class WebFetchConfig(BaseModel):
    """Web fetch (page retrieval) tool configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    timeout_seconds: int = Field(default=30, ge=1)
    max_response_bytes: int = Field(default=1_048_576, ge=1)  # 1 MB


class WebToolsConfig(BaseModel):
    """Web-related tools configuration."""

    model_config = ConfigDict(extra="forbid")

    search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    fetch: WebFetchConfig = Field(default_factory=WebFetchConfig)


class ExecToolConfig(BaseModel):
    """Shell execution tool configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    timeout_seconds: int = Field(default=60, ge=1)
    allowed_env: list[str] = Field(default_factory=lambda: ["PATH", "HOME", "USER", "LANG"])
    pattern_whitelist: list[str] = Field(default_factory=list)
    pattern_blacklist: list[str] = Field(default_factory=list)


class McpServerConfig(BaseModel):
    """Configuration for a single MCP server."""

    model_config = ConfigDict(extra="forbid")

    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    reconnect_interval_seconds: float = Field(default=5.0, ge=1.0)


class McpConfig(BaseModel):
    """MCP (Model Context Protocol) tool configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    servers: list[McpServerConfig] = Field(default_factory=list)


class ToolsConfig(BaseModel):
    """Aggregate tool subsystem configuration."""

    model_config = ConfigDict(extra="forbid")

    filesystem_enabled: bool = True
    allowed_dirs: list[Path] = Field(default_factory=list)
    shell: ExecToolConfig = Field(default_factory=ExecToolConfig)
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    sandbox_enabled: bool = False
    sandbox_command: str = "bwrap"
    mcp: McpConfig = Field(default_factory=McpConfig)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

class MemoryConfig(BaseModel):
    """Memory system configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    db_path: str = "memory.db"
    embedding_model: str = ""
    max_working_tokens: int = Field(default=8192, ge=256)
    long_term_max_tokens: int = Field(default=4096, ge=256)
    compaction_usage_ratio: float = Field(default=0.8, ge=0.0, le=1.0)
    compaction_msg_threshold: int = Field(default=20, ge=5)
    hybrid_retrieval_k: int = Field(default=10, ge=1)


# ---------------------------------------------------------------------------
# Agent defaults
# ---------------------------------------------------------------------------

class AgentDefaultConfig(BaseModel):
    """Default Agent runtime parameters."""

    model_config = ConfigDict(extra="forbid")

    model: str = ""
    max_iterations: int = Field(default=3, ge=1, le=100)
    timeout_seconds: int = Field(default=60, ge=1)
    checkpoint_interval: int = Field(default=2, ge=1)
    max_concurrent_sessions: int = Field(default=20, ge=1)
    max_subagents: int = Field(default=5, ge=0)


# ---------------------------------------------------------------------------
# HeartBeat
# ---------------------------------------------------------------------------

class HeartBeatConfig(BaseModel):
    """Heartbeat / cron engine configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    db_path: str = "heartbeat.db"


# ---------------------------------------------------------------------------
# MXWConfig root
# ---------------------------------------------------------------------------

class MXWConfig(BaseModel):
    """Root configuration model — all subsystems in one place."""

    model_config = ConfigDict(extra="forbid")

    # ── identity ──
    workspace: Path = Field(default=Path(".mxwbot"), description="Runtime root directory")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # ── sub-systems ──
    providers: list[ProviderConfig] = Field(default_factory=list)
    channels: list[ChannelConfig] = Field(default_factory=list)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    agent: AgentDefaultConfig = Field(default_factory=AgentDefaultConfig)
    heartbeat: HeartBeatConfig = Field(default_factory=HeartBeatConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)

    dump_reveal_secrets: bool = Field(
        default=False, exclude=True, description="Runtime flag — when True, dump includes secrets"
    )

    @model_validator(mode="after")
    def _validate_workspace(self) -> MXWConfig:
        if self.workspace.is_absolute() and not str(self.workspace).startswith("/"):
            # On Windows, Path(".").resolve() could lead to C:\..., so keep as-is
            pass
        return self

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """Override to optionally reveal secrets (e.g. for config debugging).

        Pass ``reveal_secrets=True`` to keep SecretStr values in the output.
        """
        reveal = kwargs.pop("reveal_secrets", self.dump_reveal_secrets)
        result = super().model_dump(**kwargs)
        if reveal:
            result = self._unmask_secrets(result)
        else:
            result = self._redact_secrets(result)
        return result

    @staticmethod
    def _redact_secrets(data: dict[str, Any]) -> dict[str, Any]:
        """Recursively replace SecretStr values with '***'."""
        out: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                out[k] = MXWConfig._redact_secrets(v)
            elif isinstance(v, list):
                out[k] = [
                    MXWConfig._redact_secrets(i) if isinstance(i, dict) else i
                    for i in v
                ]
            elif isinstance(v, SecretStr):
                out[k] = "***"
            else:
                out[k] = v
        return out

    @staticmethod
    def _unmask_secrets(data: dict[str, Any]) -> dict[str, Any]:
        """Recursively convert SecretStr values to plain strings."""
        out: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                out[k] = MXWConfig._unmask_secrets(v)
            elif isinstance(v, list):
                out[k] = [
                    MXWConfig._unmask_secrets(i) if isinstance(i, dict) else i
                    for i in v
                ]
            elif isinstance(v, SecretStr):
                out[k] = v.get_secret_value()
            else:
                out[k] = v
        return out
