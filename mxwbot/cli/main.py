"""CLI entry point for MXWbot — Typer-based command tree.

Commands:
  mxwbot serve              Start all components
  mxwbot channel start/stop/list
  mxwbot heartbeat start/stop/run/status
  mxwbot cron add/remove/enable/disable/run/list/show/status
  mxwbot watch               Rich live monitor (requires serve running)
  mxwbot config validate/show
  mxwbot skill list
"""

from __future__ import annotations

import typer

from mxwbot.cli.channel_cmd import channel_app
from mxwbot.cli.config_cmd import config_app
from mxwbot.cli.cron_cmd import cron_app
from mxwbot.cli.heartbeat_cmd import heartbeat_app
from mxwbot.cli.serve import serve_cmd
from mxwbot.cli.skill_cmd import skill_app
from mxwbot.cli.watch_cmd import watch_cmd

app = typer.Typer(
    name="mxwbot",
    help="MXWbot — lightweight multi-channel chatbot framework",
)


@app.command()
def serve(
    config: str = typer.Option("config.yaml", "--config", "-c", help="Path to config file"),
    watch: bool = typer.Option(False, "--watch", "-w", help="Open watch panel"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging"),
) -> None:
    """Start all components (production mode)."""
    import asyncio
    asyncio.run(serve_cmd(config, watch, verbose))


app.add_typer(channel_app, name="channel", help="Manage channels")
app.add_typer(heartbeat_app, name="heartbeat", help="Manage heartbeat service")
app.add_typer(cron_app, name="cron", help="Manage scheduled jobs")
app.add_typer(config_app, name="config", help="Configuration utilities")
app.add_typer(skill_app, name="skill", help="Skill management")


@app.command()
def watch() -> None:
    """Open the Rich live monitor (requires serve running)."""
    import asyncio
    asyncio.run(watch_cmd())


if __name__ == "__main__":
    app()
