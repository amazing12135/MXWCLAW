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

from mxwbot.cli.agent_cmd import agent_cmd as _agent_cmd
from mxwbot.cli.channel_cmd import channel_app
from mxwbot.cli.config_cmd import config_app
from mxwbot.cli.cron_cmd import cron_app
from mxwbot.cli.heartbeat_cmd import heartbeat_app
from mxwbot.cli.onboard_cmd import onboard_cmd as _onboard_cmd
from mxwbot.cli.serve import serve_cmd
from mxwbot.cli.skill_cmd import skill_app
from mxwbot.cli.watch_cmd import watch_cmd

app = typer.Typer(
    name="mxwbot",
    context_settings={"help_option_names": ["-h", "--help"]},
    help="MXWbot — lightweight multi-channel chatbot framework",
    no_args_is_help=True
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
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Single-turn message to send"),
    session: str = typer.Option("cli:direct", "--session", "-s", help="Session ID (channel:chat_id)"),
    config: str = typer.Option(None, "--config", "-c", help="Path to config file"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render output as Markdown"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging"),
) -> None:
    """Chat with the AI agent (interactive REPL or single message)."""
    import asyncio
    asyncio.run(_agent_cmd(message, session, config, markdown, verbose))


@app.command()
def onboard(
    workspace: str = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str = typer.Option(None, "--config", "-c", help="Path to config file"),
    wizard: bool = typer.Option(False, "--wizard", help="Interactive setup wizard"),
) -> None:
    """Initialize config.yaml and workspace."""
    import asyncio
    asyncio.run(_onboard_cmd(workspace, config, wizard))


@app.command()
def watch() -> None:
    """Open the Rich live monitor (requires serve running)."""
    import asyncio
    asyncio.run(watch_cmd())


if __name__ == "__main__":
    app()
