"""config subcommands — validate and inspect configuration."""

from pathlib import Path

import typer

from mxwbot.config.loader import load_config

config_app = typer.Typer()


@config_app.command("validate")
def validate(
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Path to config file"),
):
    """Validate config.yaml syntax and schema."""
    try:
        cfg = load_config(Path(config_path))
        typer.echo(f"✓ Config valid: {len(cfg.providers)} providers, {len(cfg.channels)} channels")
    except Exception as e:
        typer.echo(f"✗ Config invalid: {e}", err=True)
        raise typer.Exit(1)


@config_app.command("show")
def show(
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Path to config file"),
):
    """Print current config (secrets redacted)."""
    import json

    cfg = load_config(Path(config_path))
    data = cfg.model_dump()
    # Redact secrets
    for p in data.get("providers", []):
        if "api_key" in p:
            p["api_key"] = "***"
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))
