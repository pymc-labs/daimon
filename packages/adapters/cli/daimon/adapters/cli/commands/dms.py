"""Operator-only offline import of earlier private conversations."""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from daimon.adapters.cli.runtime import build_runtime
from daimon.core.config import load_settings
from daimon.core.dm_carry_over import MAX_EXPORT_BYTES, CarryOverExport, build_carry_over
from daimon.core.stores.dm_carry_overs import has_conversation, put_carry_over
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

dms_app = typer.Typer(help="Import earlier private conversations from another deployment.")


async def import_carry_overs(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    export: CarryOverExport,
) -> Counter[str]:
    """Hold each person's earlier conversation for their first DM, in one transaction.

    Counts `new`, `updated`, `consumed` (already used, never brought back) and
    `existing` (the person already has a private conversation here, so it is skipped).
    """
    counts: Counter[str] = Counter()
    async with sessionmaker.begin() as db:
        tenant = await get_tenant(db, tenant_id)
        if tenant is None:
            raise typer.BadParameter("No such tenant", param_hint="--tenant")
        if tenant.platform != "discord" or tenant.external_id != export.guild_id:
            raise typer.BadParameter(
                "File is for a different Discord server", param_hint="export_file"
            )
        for user in export.users:
            if await has_conversation(
                db, tenant_id=tenant_id, platform="discord", external_user_id=user.external_user_id
            ):
                counts["existing"] += 1
                continue
            built = build_carry_over(
                user.turns, source_name=export.source_name, exported_at=export.exported_at
            )
            if not built.history:
                counts["empty"] += 1
                continue
            counts[
                await put_carry_over(
                    db,
                    tenant_id=tenant_id,
                    platform="discord",
                    external_user_id=user.external_user_id,
                    source_name=export.source_name,
                    exported_at=export.exported_at,
                    history=built.history,
                    context=built.context,
                )
            ] += 1
    return counts


@dms_app.command("import")
def import_command(
    export_files: Annotated[list[Path], typer.Argument(metavar="EXPORT_FILE...")],
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
) -> None:
    """Import v1 JSON files of earlier conversations into --tenant; each file is one transaction.

    A person's first private conversation here starts from theirs, once.
    """
    exports: list[CarryOverExport] = []
    for path in export_files:
        if path.stat().st_size > MAX_EXPORT_BYTES:
            raise typer.BadParameter(f"{path.name} exceeds 1 MiB")
        exports.append(CarryOverExport.model_validate_json(path.read_bytes()))

    async def run() -> None:
        async with build_runtime(load_settings()) as runtime:
            for path, export in zip(export_files, exports, strict=True):
                counts = await import_carry_overs(
                    runtime.sessionmaker, tenant_id=tenant, export=export
                )
                typer.echo(
                    f"{path.name}: {counts['new']} new, {counts['updated']} updated, "
                    f"{counts['consumed']} already used, {counts['existing']} already have a "
                    f"private conversation, {counts['empty']} empty"
                )

    asyncio.run(run())
