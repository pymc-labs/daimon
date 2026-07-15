"""Destructive-op confirmation for admin commands."""

from __future__ import annotations

import typer
from rich.console import Console


def confirm_or_abort(console: Console, message: str, *, yes: bool) -> None:
    if yes:
        return
    if not typer.confirm(message, default=False):
        console.print("[dim]aborted[/dim]")
        raise typer.Exit(code=1)
