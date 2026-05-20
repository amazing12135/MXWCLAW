"""Tests for config/loader.py — YAML/ENV loading and hot reload."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from mxwbot.config.loader import (
    ConfigLoadError,
    _coerce_value,
    _env_override,
    get_config,
    load_config,
    reload_config,
)
from mxwbot.config.schema import MXWConfig


class TestCoerceValue:
    def test_bool_true(self):
        assert _coerce_value("true") is True
        assert _coerce_value("TRUE") is True
        assert _coerce_value("yes") is True

    def test_bool_false(self):
        assert _coerce_value("false") is False
        assert _coerce_value("no") is False

    def test_int(self):
        assert _coerce_value("42") == 42

    def test_float(self):
        assert _coerce_value("3.14") == 3.14

    def test_str_fallback(self):
        assert _coerce_value("hello") == "hello"


class TestEnvOverride:
    def test_simple_key(self):
        data: dict = {}
        with patch.dict(os.environ, {"MXWBOT_WORKSPACE": "/tmp/ws"}, clear=True):
            _env_override(data)
        assert data["workspace"] == "/tmp/ws"

    def test_numeric_list_index(self):
        data: dict = {"providers": []}
        with patch.dict(
            os.environ,
            {
                "MXWBOT_PROVIDERS__0__NAME": "openai",
                "MXWBOT_PROVIDERS__0__API_KEY": "sk-123",
            },
            clear=True,
        ):
            _env_override(data)
        assert data["providers"][0]["name"] == "openai"
        assert data["providers"][0]["api_key"] == "sk-123"

    def test_double_underscore(self):
        data: dict = {"channels": [{}]}
        with patch.dict(
            os.environ,
            {"MXWBOT_CHANNELS__0__TYPE": "discord", "MXWBOT_CHANNELS__0__ENABLED": "true"},
            clear=True,
        ):
            _env_override(data)
        assert data["channels"][0]["type"] == "discord"
        assert data["channels"][0]["enabled"] is True


class TestLoadConfig:
    def test_load_from_yaml_file(self, tmp_workspace, sample_yaml_config):
        yaml_path = tmp_workspace / "config.yaml"
        yaml_path.write_text(sample_yaml_config, encoding="utf-8")
        with patch("mxwbot.config.loader._config_cache", None), patch(
            "mxwbot.config.loader._config_path", None
        ):
            config = load_config(str(yaml_path))
        assert config.log_level == "DEBUG"
        assert len(config.providers) == 2
        assert config.providers[0].name == "openai"
        email_ch = next(c for c in config.channels if c.type == "email")
        assert email_ch.enabled is True
        assert email_ch.settings["username"] == "test@example.com"
        assert config.agent.max_iterations == 5

    def test_load_with_env_override(self, tmp_workspace, sample_yaml_config):
        yaml_path = tmp_workspace / "config.yaml"
        yaml_path.write_text(sample_yaml_config, encoding="utf-8")
        with patch.dict(
            os.environ,
            {"MXWBOT_LOG_LEVEL": "ERROR"},
            clear=True,
        ):
            with patch("mxwbot.config.loader._config_cache", None), patch(
                "mxwbot.config.loader._config_path", None
            ):
                config = load_config(str(yaml_path))
        assert config.log_level == "ERROR"  # ENV beats YAML

    def test_load_defaults_when_file_missing(self, tmp_workspace):
        nonexistent = tmp_workspace / "nonexistent.yaml"
        with patch("mxwbot.config.loader._config_cache", None), patch(
            "mxwbot.config.loader._config_path", None
        ):
            config = load_config(str(nonexistent))
        assert isinstance(config, MXWConfig)
        assert config.log_level == "INFO"

    def test_get_config_before_load_raises(self):
        with patch("mxwbot.config.loader._config_cache", None):
            with pytest.raises(ConfigLoadError):
                get_config()

    def test_get_config_after_load(self, tmp_workspace):
        yaml_path = tmp_workspace / "config.yaml"
        yaml_path.write_text("log_level: WARNING", encoding="utf-8")
        with patch("mxwbot.config.loader._config_cache", None), patch(
            "mxwbot.config.loader._config_path", None
        ):
            load_config(str(yaml_path))
            c = get_config()
        assert c.log_level == "WARNING"

    def test_reload(self, tmp_workspace):
        yaml_path = tmp_workspace / "config.yaml"
        yaml_path.write_text("log_level: INFO", encoding="utf-8")
        with patch("mxwbot.config.loader._config_cache", None), patch(
            "mxwbot.config.loader._config_path", None
        ):
            load_config(str(yaml_path))
            yaml_path.write_text("log_level: ERROR", encoding="utf-8")
            c2 = reload_config()
        assert c2.log_level == "ERROR"

    def test_invalid_yaml_raises(self, tmp_workspace):
        yaml_path = tmp_workspace / "bad.yaml"
        yaml_path.write_text(":: bad yaml ::", encoding="utf-8")
        with patch("mxwbot.config.loader._config_cache", None), patch(
            "mxwbot.config.loader._config_path", None
        ):
            with pytest.raises(ConfigLoadError):
                load_config(str(yaml_path))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
