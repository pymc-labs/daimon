"""Operator-only offline import of people who opted out of being tagged."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Annotated, Literal

import typer
from daimon.adapters.cli.runtime import build_runtime
from daimon.core.config import load_settings
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.tag_opt_outs import is_opted_out, set_opt_out
from daimon.core.stores.tenants import get_tenant
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MAX_MANIFEST_BYTES = 1048576

tags_app = typer.Typer(help="Import tag opt-outs from another deployment.")

DiscordId = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,20}$")]


class TagOptOutManifest(BaseModel):
    """v1 file: the Discord server and the members who never want to be tagged."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    guild_id: DiscordId
    discord_ids: list[DiscordId]


async def import_opt_outs(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    manifest: TagOptOutManifest,
) -> tuple[int, int]:
    """Opt out each distinct id in one transaction; returns (new, already). Safe to repeat."""
    new = already = 0
    async with sessionmaker.begin() as db:
        tenant = await get_tenant(db, tenant_id)
        if tenant is None:
            raise typer.BadParameter("No such tenant", param_hint="--tenant")
        if tenant.platform != "discord" or tenant.external_id != manifest.guild_id:
            raise typer.BadParameter(
                "File is for a different Discord server", param_hint="export_file"
            )
        for external_id in dict.fromkeys(manifest.discord_ids):
            principal = await get_or_create_platform_principal(
                db, tenant_id=tenant_id, platform="discord", external_id=external_id
            )
            if await is_opted_out(db, account_id=principal.account_id):
                already += 1
                continue
            await set_opt_out(db, account_id=principal.account_id, tenant_id=tenant_id)
            new += 1
    return new, already


@tags_app.command("import")
def import_command(
    export_file: Path,
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
) -> None:
    """Import a v1 JSON file of Discord ids into --tenant's tag opt-outs, in one transaction."""
    if export_file.stat().st_size > MAX_MANIFEST_BYTES:
        raise typer.BadParameter("Opt-out file exceeds 1 MiB")
    manifest = TagOptOutManifest.model_validate_json(export_file.read_bytes())

    async def run() -> tuple[int, int]:
        async with build_runtime(load_settings()) as runtime:
            return await import_opt_outs(runtime.sessionmaker, tenant_id=tenant, manifest=manifest)

    new, already = asyncio.run(run())
    typer.echo(f"{new} new, {already} already opted out")
