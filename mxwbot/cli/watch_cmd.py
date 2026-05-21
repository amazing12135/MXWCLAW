"""watch command — open Rich live monitor via ManagementAPI."""

import asyncio
import json

import httpx
import typer

API = "http://127.0.0.1:9090"


async def watch_cmd() -> None:
    """Poll the management API and display a live dashboard."""
    try:
        from rich.console import Console
        from rich.live import Live
        from rich.table import Table
    except ImportError:
        typer.echo("rich not installed. Run: pip install rich", err=True)
        raise typer.Exit(1)

    console = Console()

    def _fetch() -> dict:
        try:
            r = httpx.get(f"{API}/api/status", timeout=2)
            return r.json()
        except Exception:
            return {"error": "Cannot reach serve process"}

    def _build(data: dict) -> Table:
        t = Table(title="MXWbot Monitor", expand=True)
        t.add_column("Key", style="cyan")
        t.add_column("Value", style="green")
        if "error" in data:
            t.add_row("Status", f"[red]{data['error']}[/red]")
            return t
        t.add_row("LoopPool", "running" if data.get("loop_pool") else "stopped")
        for name, state in (data.get("components") or {}).items():
            icon = "✓" if state == "running" else "✗" if state == "error" else "⋯"
            t.add_row(f"  {icon} {name}", state)
        return t

    with Live(_build(_fetch()), console=console, refresh_per_second=1) as live:
        while True:
            await asyncio.sleep(1)
            live.update(_build(_fetch()))
