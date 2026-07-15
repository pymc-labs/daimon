"""Shared Typer options so --json / --yes / --trace help text stays uniform."""

from __future__ import annotations

import typer

JSON_OPTION = typer.Option(..., "--json", help="Emit JSON instead of a table.")
YES_OPTION = typer.Option(..., "--yes", "-y", help="Skip the confirmation prompt.")
TRACE_OPTION = typer.Option(..., "--trace", help="Verbose event output (chat only).")
