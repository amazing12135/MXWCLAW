"""Rich terminal watch panel — live system monitoring."""

from __future__ import annotations

import asyncio
from typing import Any

from mxwbot.watch.metrics import MetricsCollector, SystemSnapshot


class WatchPanel:
    """Live Rich TUI showing system status.

    Args:
        collector: ``MetricsCollector`` wired to ``SystemManager``.
        refresh_interval: Seconds between refreshes.
    """

    def __init__(
        self,
        collector: MetricsCollector,
        refresh_interval: float = 1.0,
    ) -> None:
        self._collector = collector
        self._refresh_interval = refresh_interval
        self._running = False

    async def start(self) -> None:
        """Start the live display (blocking)."""
        try:
            from rich.console import Console
            from rich.live import Live
            from rich.table import Table
        except ImportError:
            print("rich not installed. Run: pip install rich")
            return

        console = Console()
        self._running = True

        def _build(snap: SystemSnapshot) -> Table:
            t = Table(title="MXWbot v0.1.0", expand=True)
            t.add_column("Metric", style="cyan")
            t.add_column("Value", style="green")
            t.add_row("Uptime", f"{snap.uptime_seconds:.0f}s")
            t.add_row("Sessions", str(snap.active_sessions))
            t.add_row("Queue Depth", str(snap.queue_depth_input))
            t.add_row("Cron Jobs", str(snap.cron_jobs))
            if snap.cron_next_wake_s:
                t.add_row("Cron Next Wake", f"{snap.cron_next_wake_s:.0f}s")
            t.add_section()
            t.add_row("Components", "")
            for name, state in snap.components.items():
                icon = "✓" if state == "running" else "✗" if state == "error" else "⋯"
                color = "green" if state == "running" else "red" if state == "error" else "yellow"
                t.add_row(f"  {icon} {name}", f"[{color}]{state}[/{color}]")
            return t

        with Live(_build(self._collector.collect()), console=console, refresh_per_second=2) as live:
            while self._running:
                await asyncio.sleep(self._refresh_interval)
                live.update(_build(self._collector.collect()))

    def stop(self) -> None:
        self._running = False


# -- keep the legacy WatchPanel alias for compatibility --
# (design spec references WatchPanel as the main class)

__all__ = ["WatchPanel"]
