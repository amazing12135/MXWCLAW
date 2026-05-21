"""channel subcommands — control active channels via ManagementAPI."""

import httpx
import typer

API = "http://127.0.0.1:9090"

channel_app = typer.Typer()


def _post(path: str, **params) -> dict:
    try:
        r = httpx.post(f"{API}{path}", params=params, timeout=5)
        return r.json()
    except Exception:
        typer.echo("Cannot reach mxwbot serve process. Is it running?", err=True)
        raise typer.Exit(1)


def _get(path: str) -> dict:
    try:
        r = httpx.get(f"{API}{path}", timeout=5)
        return r.json()
    except Exception:
        typer.echo("Cannot reach mxwbot serve process. Is it running?", err=True)
        raise typer.Exit(1)


@channel_app.command("start")
def start(name: str = typer.Argument(..., help="Channel name: wechat, qq, or email")):
    """Start a channel."""
    r = _post("/api/channel/start", name=name)
    if r.get("status") == "ok":
        typer.echo(f"✓ channel {name} started")
    else:
        typer.echo(f"✗ failed", err=True)
        raise typer.Exit(1)


@channel_app.command("stop")
def stop(name: str = typer.Argument(..., help="Channel name: wechat, qq, or email")):
    """Stop a channel."""
    _post("/api/channel/stop", name=name)
    typer.echo(f"✓ channel {name} stopped")


@channel_app.command("list")
def list_channels():
    """List all configured channels and their state."""
    r = _get("/api/channel/list")
    for ch in r.get("channels", []):
        icon = "✓" if ch["state"] == "running" else "✗" if ch["state"] == "error" else "⋯"
        typer.echo(f"  {icon}  {ch['name']:20}  {ch['state']}")
