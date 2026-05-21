"""Watch panel for MXWbot — live Rich TUI monitoring."""

from mxwbot.watch.metrics import MetricsCollector, SystemSnapshot
from mxwbot.watch.panel import WatchPanel

__all__ = ["MetricsCollector", "SystemSnapshot", "WatchPanel"]
