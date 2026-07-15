"""`daimon help agents` — print the embedded agentic-CLI reference."""

from __future__ import annotations

import importlib.resources
import sys

import typer

help_app = typer.Typer(help="Reference material")


@help_app.command("agents")
def help_agents() -> None:
    try:
        content = (
            importlib.resources.files("daimon.adapters.cli.resources")
            .joinpath("agents.md")
            .read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        print("agents.md not found in the installed package.", file=sys.stderr)
        raise typer.Exit(code=1) from None
    sys.stdout.write(content)
