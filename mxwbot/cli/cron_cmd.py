"""cron subcommands — manage scheduled jobs via ManagementAPI."""

import httpx
import typer

API = "http://127.0.0.1:9090"

cron_app = typer.Typer()


def _post(path: str, json_data: dict | None = None) -> dict:
    try:
        r = httpx.post(f"{API}{path}", json=json_data, timeout=5)
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


def _delete(path: str) -> dict:
    try:
        r = httpx.delete(f"{API}{path}", timeout=5)
        return r.json()
    except Exception:
        typer.echo("Cannot reach mxwbot serve process.", err=True)
        raise typer.Exit(1)


@cron_app.command("add")
def add(
    name: str = typer.Argument(..., help="Job display name"),
    message: str = typer.Argument(..., help="Message sent to the agent when the job fires"),
    at: str = typer.Option(None, "--at", help="One-shot: ISO datetime, e.g. '2026-06-01T09:00:00'"),
    every: str = typer.Option(None, "--every", help="Interval: e.g. '30m', '1h', '1d'"),
    cron: str = typer.Option(None, "--cron", help="Crontab expression: e.g. '0 9 * * *'"),
    tz: str = typer.Option(None, "--tz", help="Timezone (cron only)"),
    deliver: bool = typer.Option(False, "--deliver", help="Deliver response to channel"),
    channel: str = typer.Option(None, "--channel", help="Target channel for delivery"),
    once: bool = typer.Option(False, "--once", help="Delete after first run"),
):
    """Add a scheduled job."""
    from datetime import datetime

    schedule: dict = {}
    if at:
        try:
            at_ms = int(datetime.fromisoformat(at).timestamp() * 1000)
        except ValueError:
            typer.echo("Invalid --at format. Use ISO: '2026-06-01T09:00:00'", err=True)
            raise typer.Exit(1)
        schedule = {"kind": "at", "at_ms": at_ms}
    elif every:
        ms = _parse_every(every)
        if ms is None:
            typer.echo("Invalid --every format. Use: '30m', '1h', '1d'", err=True)
            raise typer.Exit(1)
        schedule = {"kind": "every", "every_ms": ms}
    elif cron:
        schedule = {"kind": "cron", "expr": cron, "tz": tz}
    else:
        typer.echo("Need one of: --at, --every, or --cron", err=True)
        raise typer.Exit(1)

    r = _post("/api/cron/add", json_data={
        "name": name, "message": message, "schedule": schedule,
        "deliver": deliver, "channel": channel, "delete_after_run": once,
    })
    if "error" in r:
        typer.echo(f"✗ {r['error']}", err=True)
        raise typer.Exit(1)
    next_run = r.get("next_run")
    next_str = f" → next run at {next_run}" if next_run else ""
    typer.echo(f"✓ job created: {r['id']} ({name}){next_str}")


@cron_app.command("remove")
def remove(job_id: str = typer.Argument(..., help="Job ID to remove")):
    """Remove a job by ID."""
    r = _delete(f"/api/cron/{job_id}")
    if r.get("status") == "ok":
        typer.echo(f"✓ removed {job_id}")
    else:
        typer.echo(f"✗ not found", err=True)


@cron_app.command("enable")
def enable(job_id: str = typer.Argument(..., help="Job ID to enable")):
    """Enable a disabled job."""
    _post(f"/api/cron/{job_id}/enable", json_data={"enabled": True})
    typer.echo(f"✓ enabled {job_id}")


@cron_app.command("disable")
def disable(job_id: str = typer.Argument(..., help="Job ID to disable")):
    """Disable a job (pauses it)."""
    _post(f"/api/cron/{job_id}/enable", json_data={"enabled": False})
    typer.echo(f"✓ disabled {job_id}")


@cron_app.command("run")
def run(job_id: str = typer.Argument(..., help="Job ID to run immediately")):
    """Manually trigger a job."""
    r = _post(f"/api/cron/{job_id}/run")
    typer.echo(f"✓ triggered {job_id}" if r.get("status") == "ok" else f"✗ {r.get('status')}")


@cron_app.command("show")
def show(job_id: str = typer.Argument(..., help="Job ID to inspect")):
    """Show job details."""
    r = _get(f"/api/cron/{job_id}")
    if "error" in r:
        typer.echo(f"✗ {r['error']}", err=True)
        return
    enabled = "✓" if r.get("enabled") else "✗"
    typer.echo(f"  {enabled} {r['id']:10}  {r['name']}")
    typer.echo(f"  schedule: {r.get('schedule')}")
    typer.echo(f"  next_run: {r.get('next_run_at_ms')}")
    typer.echo(f"  payload:  {r.get('payload', '')[:80]}")


@cron_app.command("list")
def list_jobs():
    """List all cron jobs."""
    r = _get("/api/cron/list")
    if not r.get("jobs"):
        typer.echo("  (no jobs)")
        return
    for j in r["jobs"]:
        icon = "✓" if j.get("enabled") else "✗"
        next_run = j.get("next_run_at_ms", "--")
        last = j.get("last_status", "--")
        typer.echo(f"  {icon} {j['id']:10}  {j['name']:20}  next={next_run}  last={last}")


@cron_app.command("status")
def cron_status():
    """Show cron service status."""
    r = _get("/api/cron/status")
    typer.echo(f"  enabled: {r.get('enabled')}")
    typer.echo(f"  jobs:    {r.get('jobs')}")
    typer.echo(f"  wake:    {r.get('next_wake_at_ms', '--')}")


def _parse_every(value: str) -> int | None:
    value = value.strip().lower()
    multipliers = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
    for suffix, mult in multipliers.items():
        if value.endswith(suffix):
            try:
                return int(float(value[:-1]) * mult)
            except (ValueError, TypeError):
                return None
    return None
