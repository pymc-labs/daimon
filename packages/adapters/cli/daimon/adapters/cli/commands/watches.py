"""Operator-only offline import and explicit binding confirmation."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import typer
from daimon.adapters.cli.runtime import build_runtime
from daimon.core.config import load_settings
from daimon.core.continuity.watch_imports import WatchExport
from daimon.core.stores.watches import confirm_import, get_watch, import_watch

watches_app = typer.Typer(
    help="Import paused watches and confirm reviewed owner/destination bindings."
)


@watches_app.command("import")
def import_command(export_file: Path) -> None:
    """Import a v1 JSON manifest, offline. Every watch starts paused; no key is copied."""
    if export_file.stat().st_size > 1048576:
        raise typer.BadParameter("Watch export exceeds 1 MiB")
    manifest = WatchExport.model_validate_json(export_file.read_bytes())

    async def run() -> None:
        async with build_runtime(load_settings()) as runtime:
            # Whole-file transaction: no half-import or lost owner notice on crash.
            async with runtime.sessionmaker.begin() as db:
                rows = [
                    await import_watch(db, entry, now=datetime.now(UTC))
                    for entry in manifest.watches
                ]
            for row in rows:
                detail = row.source_state.get("mapping_error")
                suffix = f": {detail}" if detail else ""
                typer.echo(f"{row.id} paused {row.source_state.get('reason')}{suffix}")

    asyncio.run(run())


@watches_app.command("confirm")
def confirm_command(
    watch_id: uuid.UUID,
    tenant_id: uuid.UUID,
    owner_account_id: uuid.UUID,
    agent_id: str,
    parent_channel_id: str,
    thread_id: str,
) -> None:
    """Confirm a mapped import's exact binding. Page watches first need owner key rotation."""

    async def run() -> None:
        async with build_runtime(load_settings()) as runtime:
            async with runtime.sessionmaker.begin() as db:
                current = await get_watch(db, watch_id)
                if current is not None and current.source_state.get("tool") == "read_thread":
                    typer.echo(
                        "Warning: imported message reads require a thread ID. "
                        "Channel and DM IDs are unsupported; use read_channel for channel sources.",
                        err=True,
                    )
                row = await confirm_import(
                    db,
                    watch_id,
                    tenant_id=tenant_id,
                    owner_account_id=owner_account_id,
                    agent_id=agent_id,
                    parent_channel_id=parent_channel_id,
                    thread_id=thread_id,
                    now=datetime.now(UTC),
                )
            typer.echo(f"{row.id} {row.status}")

    asyncio.run(run())
