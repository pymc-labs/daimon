"""Typer root + sub-app wiring."""

from __future__ import annotations

import importlib.metadata

import rich.traceback
import typer
from daimon.adapters.cli.commands.agents import agents_app
from daimon.adapters.cli.commands.config import config_app
from daimon.adapters.cli.commands.defaults import defaults_app
from daimon.adapters.cli.commands.environments import environments_app
from daimon.adapters.cli.commands.help import help_app
from daimon.adapters.cli.commands.mcp import mcp_app
from daimon.adapters.cli.commands.notebook import notebook_app
from daimon.adapters.cli.commands.routines import routines_app
from daimon.adapters.cli.commands.sessions import sessions_app
from daimon.adapters.cli.commands.skills import skills_app
from daimon.adapters.cli.commands.tenants import tenants_app
from daimon.adapters.cli.run.command import run_command

app = typer.Typer(help="Daimon CMA CLI")
app.add_typer(agents_app, name="agents")
app.add_typer(environments_app, name="environments")
app.add_typer(tenants_app, name="tenants")
app.add_typer(sessions_app, name="sessions")
app.add_typer(config_app, name="config")
app.add_typer(defaults_app, name="defaults")
app.add_typer(skills_app, name="skills")
app.add_typer(help_app, name="help")
app.add_typer(mcp_app, name="mcp")
app.add_typer(notebook_app, name="notebook")
app.add_typer(routines_app, name="routines")
app.command("run")(run_command)


@app.callback()
def root() -> None:
    rich.traceback.install(show_locals=False)


@app.command("version")
def version_command() -> None:
    version = importlib.metadata.version("daimon-adapter-cli")
    typer.echo(f"daimon {version}")


if __name__ == "__main__":
    app()
