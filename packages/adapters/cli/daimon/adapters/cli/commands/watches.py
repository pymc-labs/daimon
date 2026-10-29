"""Operator-only offline import and explicit binding confirmation."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.runtime import build_runtime
from daimon.core.config import load_settings
from daimon.core.continuity.watch_imports import WatchExport
from daimon.core.stores.platform_names import get_user_names
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.watches import (
    WatchActivity,
    confirm_import,
    get_watch,
    import_watch,
    list_tenant_watches,
)

watches_app = typer.Typer(
    help="List a tenant's watches, import paused watches and confirm reviewed bindings."
)


def _listed(item: WatchActivity, names: dict[tuple[str, str], str | None]) -> dict[str, object]:
    """The operator view of one watch. Never its instructions, keys, baseline or evidence."""
    row = item.watch
    reason, tool = row.source_state.get("reason"), row.source_state.get("tool")
    return {
        "watch_id": str(row.id),
        "owner_id": row.owner_external_id,
        "owner": names.get((row.platform, row.owner_external_id)),
        "status": row.status,
        "reason": str(reason) if reason else None,
        "source": row.source,
        "source_tool": str(tool) if tool else None,
        "destination": f"{row.platform} thread",
        "parent_channel_id": row.parent_channel_id,
        "thread_id": row.thread_id,
        "agent_id": row.agent_id,
        "expires_at": row.expires_at.isoformat(),
        "fires_today": item.fires_today,
        "last_fired_at": item.last_fired_at.isoformat() if item.last_fired_at else None,
        "errors": row.consecutive_errors,
        "retry_after": row.backoff_until.isoformat() if row.backoff_until else None,
    }


@watches_app.command("list")
def list_command(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """List every watch in --tenant, read-only: owner, status, source, expiry and firings."""

    async def run() -> list[dict[str, object]]:
        async with build_runtime(load_settings()) as runtime, runtime.sessionmaker() as db:
            if await get_tenant(db, tenant) is None:
                raise typer.BadParameter("No such tenant", param_hint="--tenant")
            listed = await list_tenant_watches(db, tenant_id=tenant, now=datetime.now(UTC))
            names: dict[tuple[str, str], str | None] = {}
            for platform in {item.watch.platform for item in listed}:
                found = await get_user_names(
                    db,
                    tenant_id=tenant,
                    platform=platform,
                    user_ids={
                        item.watch.owner_external_id
                        for item in listed
                        if item.watch.platform == platform
                    },
                )
                names.update({(platform, user): name.label for user, name in found.items()})
        return [_listed(item, names) for item in listed]

    rows = asyncio.run(run())
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("No watches.")
        return
    for row in rows:
        owner = f"{row['owner']} ({row['owner_id']})" if row["owner"] else row["owner_id"]
        status = f"{row['status']}: {row['reason']}" if row["reason"] else row["status"]
        source = f"{row['source']}/{row['source_tool']}" if row["source_tool"] else row["source"]
        last = row["last_fired_at"] or "never"
        typer.echo(
            f"{row['watch_id']}  owner={owner}  status={status}  source={source}  "
            f"to={row['destination']} {row['thread_id']}  expires={row['expires_at']}  "
            f"fires_today={row['fires_today']}  last_fired={last}  errors={row['errors']}"
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
