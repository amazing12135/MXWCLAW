"""onboard command — initialise config.yaml and workspace."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt

console = Console()

_DEFAULT_CONFIG_YAML = """# MXWbot configuration
workspace: .mxwbot
log_level: INFO

providers:
  - name: openai
    api_key: "sk-your-key-here"
    model: gpt-4
    max_tokens: 4096
    temperature: 0.7

channels:
  - type: wechat
    enabled: false
  - type: email
    enabled: false

tools:
  filesystem_enabled: true
  shell:
    enabled: true
    timeout_seconds: 30

agent:
  max_iterations: 5
  timeout_seconds: 120
  checkpoint_interval: 2
"""

_HEARTBEAT_TEMPLATE = """# Heartbeat Tasks

Add tasks you want the agent to check periodically.
Example:

- Check for new emails every morning
- Monitor server status at 9:00 AM
- Review pull requests at 6:00 PM

Leave empty to disable heartbeat task checking.
"""


def _create_default_config(config_path: Path) -> None:
    config_path.write_text(_DEFAULT_CONFIG_YAML, encoding="utf-8")
    console.print(f"[green]✓[/green] Created config at {config_path}")


def _create_workspace(workspace: Path) -> None:
    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace}")

    # Template files
    heartbeat_file = workspace / "HEARTBEAT.md"
    if not heartbeat_file.exists():
        heartbeat_file.write_text(_HEARTBEAT_TEMPLATE, encoding="utf-8")
        console.print(f"[green]✓[/green] Created HEARTBEAT.md template")

    # Sessions dir
    (workspace / "sessions").mkdir(exist_ok=True)
    (workspace / "cron").mkdir(exist_ok=True)
    (workspace / "skills").mkdir(exist_ok=True)


async def onboard_cmd(
    workspace: str | None = None,
    config_path: str | None = None,
    wizard: bool = False,
) -> None:
    """Initialise or refresh MXWbot configuration and workspace.

    Without --wizard: creates config.yaml with sensible defaults if none exists.
    With --wizard: interactive setup (choose provider, enter API key, etc.)
    """
    cfg_path = Path(config_path or "config.yaml").expanduser().resolve()
    ws_path = Path(workspace or ".mxwbot").expanduser().resolve()

    console.print("MXWbot Setup\n")

    if wizard:
        await _run_wizard(cfg_path, ws_path)
    else:
        await _run_quickstart(cfg_path, ws_path)

    console.print(f"\n[bold]Done![/bold] Start with: [cyan]mxwbot agent[/cyan]")


async def _run_quickstart(cfg_path: Path, ws_path: Path) -> None:
    """Non-interactive: create defaults if missing."""
    if cfg_path.exists():
        console.print(f"[dim]Config exists at {cfg_path}[/dim]")
        if Confirm.ask("Overwrite with defaults? (existing values will be lost)"):
            _create_default_config(cfg_path)
    else:
        _create_default_config(cfg_path)

    _create_workspace(ws_path)

    console.print("\nNext steps:")
    console.print(f"  1. Edit [cyan]{cfg_path}[/cyan] and set your API key")
    console.print(f"  2. Chat: [cyan]mxwbot agent[/cyan]")


async def _run_wizard(cfg_path: Path, ws_path: Path) -> None:
    """Interactive setup wizard."""
    console.print("[bold]Interactive Setup Wizard[/bold]\n")

    # Provider selection
    console.print("Select your LLM provider:")
    provider = Prompt.ask("  Provider", choices=["openai", "anthropic", "deepseek"], default="openai")

    api_key = Prompt.ask("  API Key", password=True)

    model_defaults = {"openai": "gpt-4", "anthropic": "claude-sonnet-4-6", "deepseek": "deepseek-chat"}
    model = Prompt.ask("  Model", default=model_defaults.get(provider, "gpt-4"))

    # Workspace
    workspace_input = Prompt.ask("  Workspace directory", default=str(ws_path))
    ws_path = Path(workspace_input).expanduser().resolve()

    # Generate config
    config_yaml = f"""# MXWbot configuration
workspace: {ws_path}
log_level: INFO

providers:
  - name: {provider}
    api_key: "{api_key}"
    model: {model}
    max_tokens: 4096
    temperature: 0.7

channels:
  - type: wechat
    enabled: false
  - type: email
    enabled: false

tools:
  filesystem_enabled: true
  shell:
    enabled: true
    timeout_seconds: 30

agent:
  max_iterations: 5
  timeout_seconds: 120
  checkpoint_interval: 2
"""
    cfg_path.write_text(config_yaml, encoding="utf-8")
    console.print(f"\n[green]✓[/green] Config saved to {cfg_path}")

    _create_workspace(ws_path)
