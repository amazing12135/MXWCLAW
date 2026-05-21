"""skill subcommands — list loaded skills (offline)."""

from pathlib import Path

import typer

from mxwbot.core.skill import SkillLoader

skill_app = typer.Typer()


@skill_app.command("list")
def list_skills(
    workspace: str = typer.Option(".mxwbot", "--workspace", "-w", help="Workspace path"),
):
    """List all .md skills found in the workspace."""
    loader = SkillLoader(Path(workspace) / "skills")
    skills = loader.scan()
    if not skills:
        typer.echo("  (no skills found)")
        return
    for s in skills:
        avail = "always" if s.availability == "always" else "manual"
        typer.echo(f"  {s.name:20}  [{avail}]  {s.description[:60]}")
