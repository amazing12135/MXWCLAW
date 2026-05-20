"""Tests for config/schema.py — Pydantic model validation."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from mxwbot.config.schema import (
    AgentDefaultConfig,
    ChannelConfig,
    ExecToolConfig,
    GatewayConfig,
    HeartBeatConfig,
    McpConfig,
    McpServerConfig,
    MemoryConfig,
    MXWConfig,
    ProviderConfig,
    ToolsConfig,
    WebFetchConfig,
    WebSearchConfig,
    WebToolsConfig,
)


class TestProviderConfig:
    def test_defaults(self):
        c = ProviderConfig(name="openai")
        assert c.name == "openai"
        assert c.api_key.get_secret_value() == ""
        assert c.max_tokens == 4096
        assert c.temperature == 0.7

    def test_secret_str_redacted_in_repr(self):
        c = ProviderConfig(name="deepseek", api_key=SecretStr("sk-12345"))
        s = str(c)
        assert "sk-12345" not in s

    def test_secret_str_value(self):
        c = ProviderConfig(name="deepseek", api_key=SecretStr("sk-12345"))
        assert c.api_key.get_secret_value() == "sk-12345"


class TestChannelConfig:
    def test_defaults(self):
        c = ChannelConfig(type="wechat")
        assert c.type == "wechat"
        assert c.enabled is False
        assert c.settings == {}

    def test_with_settings(self):
        c = ChannelConfig(
            type="email",
            enabled=True,
            settings={"imap_host": "imap.example.com", "imap_port": 993},
        )
        assert c.type == "email"
        assert c.enabled is True
        assert c.settings["imap_host"] == "imap.example.com"

    def test_new_channel_type_no_code_change(self):
        """Adding a new channel type just means new config data, not new class."""
        c = ChannelConfig(
            type="discord",
            enabled=True,
            settings={"token": "secret-token", "guild_id": "123"},
        )
        assert c.type == "discord"


class TestToolsConfig:
    def test_defaults(self):
        c = ToolsConfig()
        assert c.filesystem_enabled is True
        assert c.shell.enabled is True
        assert c.sandbox_enabled is False


class TestMemoryConfig:
    def test_defaults(self):
        c = MemoryConfig()
        assert c.compaction_usage_ratio == 0.8
        assert c.compaction_msg_threshold == 20


class TestAgentDefaultConfig:
    def test_defaults(self):
        c = AgentDefaultConfig()
        assert c.max_iterations == 3
        assert c.checkpoint_interval == 2
        assert c.max_concurrent_sessions == 20
        assert c.max_subagents == 5


class TestMXWConfig:
    def test_default_construction(self):
        c = MXWConfig()
        assert c.workspace == Path(".mxwbot")  # pyright: ignore[reportArgumentType] — pydantic coerces
        assert c.log_level == "INFO"
        assert c.providers == []
        assert isinstance(c.channels, list)
        assert c.channels == []
        assert isinstance(c.tools, ToolsConfig)
        assert isinstance(c.memory, MemoryConfig)
        assert isinstance(c.agent, AgentDefaultConfig)
        assert isinstance(c.heartbeat, HeartBeatConfig)
        assert isinstance(c.gateway, GatewayConfig)

    def test_secret_redaction_in_dict(self):
        c = MXWConfig(
            providers=[
                ProviderConfig(name="openai", api_key=SecretStr("sk-secret"))
            ]
        )
        d = c.model_dump()
        # Nested provider.api_key should be "***"
        assert d["providers"][0]["api_key"] == "***"

    def test_secret_reveal(self):
        c = MXWConfig(
            providers=[
                ProviderConfig(name="openai", api_key=SecretStr("sk-secret"))
            ]
        )
        d = c.model_dump(reveal_secrets=True)
        assert d["providers"][0]["api_key"] == "sk-secret"

    def test_parse_minimal_yaml(self):
        data = {"workspace": "/tmp/mxwbot", "log_level": "WARNING"}
        c = MXWConfig.model_validate(data)
        assert c.workspace == Path("/tmp/mxwbot")  # pyright: ignore[reportArgumentType]
        assert c.log_level == "WARNING"


# ---------------------------------------------------------------------------
# Ensure the file can be run directly
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
