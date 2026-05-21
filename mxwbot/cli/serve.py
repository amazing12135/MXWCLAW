"""serve command — bootstrap and start all components."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from mxwbot.config.loader import load_config
from mxwbot.system.api import ManagementAPI
from mxwbot.system.manager import SystemManager
from mxwbot.watch.metrics import MetricsCollector
from mxwbot.watch.panel import WatchPanel

logger = logging.getLogger("mxwbot.cli.serve")


async def serve_cmd(config_path: str, watch: bool = False, verbose: bool = False) -> None:
    """Bootstrap the system and start the main loop."""

    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    # Load config
    cfg = load_config(Path(config_path))
    logger.info("Config loaded: %d providers, %d channels",
                len(cfg.providers), len(cfg.channels))

    # Bootstrap
    manager = SystemManager(cfg)
    await manager.bootstrap()

    # Start management API
    api = ManagementAPI(manager)
    api.start()

    # Start watch panel if requested
    panel_task = None
    if watch:
        collector = MetricsCollector(manager)
        panel = WatchPanel(collector)
        panel_task = asyncio.create_task(panel.start())

    # Start all components
    try:
        await manager.serve()
    except asyncio.CancelledError:
        logger.info("Serve cancelled — shutting down")
    finally:
        if panel_task:
            panel_task.cancel()
        api.stop()
        await manager.shutdown()
        logger.info("MXWbot stopped")
