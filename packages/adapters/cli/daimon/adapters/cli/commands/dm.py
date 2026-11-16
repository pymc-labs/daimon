"""`daimon dm`: private conversations with agents."""

from __future__ import annotations

import typer
from daimon.adapters.cli.commands.dm_history import import_history_command

dm_app = typer.Typer(help="Private conversations with agents.")
dm_app.command("import-history")(import_history_command)
