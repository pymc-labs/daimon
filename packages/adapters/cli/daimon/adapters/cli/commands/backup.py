"""Operator recovery exports (workspace-wide, potentially secret-bearing)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from anthropic import AsyncAnthropic
from daimon.core.config import load_settings
from daimon.core.defaults.platform_export import export_platform

backup_app = typer.Typer(help="Operator backup tools for the dedicated MA workspace.")


@backup_app.command("platform-export")
def platform_export_command(destination: Path) -> None:
    """Write a private ZIP of platform objects; destination must not exist."""
    settings = load_settings()

    async def export() -> None:
        async with AsyncAnthropic(
            api_key=settings.anthropic.api_key.get_secret_value(),
            base_url=str(settings.anthropic.base_url),
        ) as client:
            await export_platform(client, destination)

    try:
        asyncio.run(export())
    except Exception:
        # SDK exceptions can contain response bodies with private workspace data.
        typer.echo("Export failed; no archive published. Check access and destination.", err=True)
        raise typer.Exit(1) from None
    typer.echo("Platform export complete.")
