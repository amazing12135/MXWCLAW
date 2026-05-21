"""heartbeat subcommands — control the LLM-driven heartbeat service."""

import httpx
import typer

API = "http://127.0.0.1:9090"

heartbeat_app = typer.Typer()


def _post(path: str) -> dict:
    try:
        r = httpx.post(f"{API}{path}", timeout=5)
        return r.json()
    except Exception:
        typer.echo("Cannot reach mxwbot serve process.", err=True)
        raise typer.Exit(1)


def _get(path: str) -> dict:
    try:
        r = httpx.get(f"{API}{path}", timeout=5)
        return r.json()
    except Exception:
        typer.echo("Cannot reach mxwbot serve process.", err=True)
        raise typer.Exit(1)


@heartbeat_app.command("start")
def start():
    """Start the heartbeat service."""
    _post("/api/heartbeat/start")
    typer.echo("✓ heartbeat started")


@heartbeat_app.command("stop")
def stop():
    """Stop the heartbeat service."""
    _post("/api/heartbeat/stop")
    typer.echo("✓ heartbeat stopped")


@heartbeat_app.command("run")
def run():
    """Manually trigger one heartbeat tick."""
    r = _post("/api/heartbeat/run")
    tasks = r.get("tasks")
    if tasks:
        typer.echo(f"Heartbeat: run — {tasks}")
    else:
        typer.echo("Heartbeat: skip (nothing to do or HEARTBEAT.md missing)")


@heartbeat_app.command("status")
def status():
    """Show heartbeat status."""
    r = _get("/api/heartbeat/status")
    typer.echo(f"  running:   {r.get('running')}")
    typer.echo(f"  enabled:   {r.get('enabled')}")
    typer.echo(f"  interval:  {r.get('interval_s')}s")
    typer.echo(f"  file:      {'exists' if r.get('file_exists') else 'missing'}")
