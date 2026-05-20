"""Configuration loader — YAML/ENV → MXWConfig.

Priority: environment variables > YAML file > Pydantic defaults.

ENV vars use ``__`` (double underscore) for nesting::

    MXWBOT_WORKSPACE            → workspace
    MXWBOT_LOG_LEVEL            → log_level
    MXWBOT_PROVIDERS__0__NAME   → providers[0]["name"]
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from mxwbot.config.schema import MXWConfig

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_config_cache: MXWConfig | None = None
_config_path: Path | None = None


class ConfigLoadError(Exception):
    """Raised when configuration cannot be loaded or is invalid."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, return an empty dict if the file is missing."""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError as exc:
        raise ConfigLoadError(f"Failed to parse YAML config at {path}: {exc}") from exc


def _env_override(data: dict[str, Any]) -> None:
    """Apply MXWBOT_* environment variables over *data* in-place."""
    prefix = "MXWBOT_"
    for key, value in os.environ.items():
        if not key.startswith(prefix) or not value:
            continue
        parts = key[len(prefix):].lower().split("__")
        _set_nested(data, parts, value)


def _set_nested(data: dict[str, Any], path: list[str], value: str) -> None:
    """Set a deeply nested value, creating intermediate dicts/lists as needed.

    Numeric parts are treated as list indices.
    """
    if not path:
        return
    current: Any = data
    for i, part in enumerate(path[:-1]):
        next_key = path[i + 1] if i + 1 < len(path) else ""
        if isinstance(current, dict):
            if next_key.isdigit():
                if part not in current or not isinstance(current[part], list):
                    current[part] = []
                current = current[part]
            else:
                if part not in current or not isinstance(current[part], dict):
                    current[part] = {}
                current = current[part]
        elif isinstance(current, list):
            idx = int(part)
            while len(current) <= idx:
                current.append({} if not next_key.isdigit() else [])
            current = current[idx]
        else:
            return
    leaf = path[-1]
    if isinstance(current, list):
        idx = int(leaf)
        while len(current) <= idx:
            current.append(None)
        current[idx] = _coerce_value(value)
    elif isinstance(current, dict):
        current[leaf] = _coerce_value(value)


def _coerce_value(raw: str) -> Any:
    """Try to convert a string to int/float/bool; keep as str on failure."""
    if raw.lower() in ("true", "yes"):
        return True
    if raw.lower() in ("false", "no"):
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _resolve_path(explicit: str | Path | None) -> Path:
    """Resolve config file path from explicit arg or default search order."""
    if explicit:
        return Path(explicit)
    env_path = os.environ.get("MXWBOT_CONFIG")
    if env_path:
        return Path(env_path)
    local = Path("config.yaml")
    if local.exists():
        return local
    home = Path.home() / ".mxwbot" / "config.yaml"
    if home.exists():
        return home
    return local


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(config_path: str | Path | None = None) -> MXWConfig:
    """Load configuration, caching the result globally.

    Args:
        config_path: Path to a YAML config file. If None, the default
                     search order is used: ``MXWBOT_CONFIG`` env var →
                     ``./config.yaml`` → ``~/.mxwbot/config.yaml``
    """
    global _config_cache, _config_path

    resolved = _resolve_path(config_path)
    _config_path = resolved

    yaml_data = _load_yaml(resolved)
    _env_override(yaml_data)

    try:
        config = MXWConfig.model_validate(yaml_data)
    except Exception as exc:
        raise ConfigLoadError(
            f"Failed to validate config from {resolved}: {exc}"
        ) from exc

    _config_cache = config
    return config


def reload_config() -> MXWConfig:
    """Reload configuration from the last-used path."""
    if _config_path is None:
        raise ConfigLoadError("No config has been loaded yet. Call load_config() first.")
    return load_config(_config_path)


def get_config() -> MXWConfig:
    """Return the currently loaded config. Raises if not loaded yet."""
    if _config_cache is None:
        raise ConfigLoadError("Configuration not loaded. Call load_config() first.")
    return _config_cache
