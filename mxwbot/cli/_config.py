"""Unified config loading — all CLI commands use this single entry point."""

from __future__ import annotations

import logging
from pathlib import Path

from mxwbot.config.loader import load_config
from mxwbot.config.schema import MXWConfig

logger = logging.getLogger("mxwbot.cli.config")


def load_runtime_config(
    config_path: str | Path | None = None,
    *,
    workspace: str | None = None,
    auto_create: bool = False,
) -> MXWConfig:
    """Load runtime configuration, shared by agent / serve / gateway.

    Args:
        config_path: Explicit path to config file (YAML).
        workspace: Override workspace directory from CLI flag.
        auto_create: If True, create a default config when none is found
            (used by ``agent`` command which should work without config).

    Returns:
        Validated ``MXWConfig`` instance.
    """
    if config_path:
        path = Path(config_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        cfg = load_config(path)
    elif auto_create:
        try:
            cfg = load_config()
        except Exception:
            cfg = MXWConfig()
            logger.info("No config found, using defaults — run 'mxwbot onboard' to set up")
    else:
        cfg = load_config()

    if workspace:
        cfg.workspace = Path(workspace).expanduser().resolve()

    return cfg
