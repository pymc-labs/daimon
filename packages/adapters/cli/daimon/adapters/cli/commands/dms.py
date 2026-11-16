"""Operator-only offline import of earlier private conversations."""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from daimon.adapters.cli.runtime import build_runtime
from daimon.core.config import load_settings
from daimon.core.dm_carry_over import (
    MAX_EXPORT_BYTES,
    CarryOverExport,
    CarryOverTurn,
    build_carry_over,
)
from daimon.core.stores.dm_carry_overs import has_conversation, put_carry_over
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

dms_app = typer.Typer(help="Import earlier private conversations from another deployment.")


def merge_users(
    exports: Sequence[CarryOverExport],
) -> dict[str, tuple[CarryOverExport, list[CarryOverTurn]]]:
    """One entry per person: their turns from every file, each turn once.

    A person split over files must come from one source and one export time, or the
    run fails: two different histories are never silently collapsed into one.
    """
    merged: dict[str, tuple[CarryOverExport, dict[tuple[str, str, datetime], CarryOverTurn]]] = {}
    for export in exports:
        for user in export.users:
            first, turns = merged.setdefault(user.external_user_id, (export, {}))
            if (first.source_name, first.exported_at) != (export.source_name, export.exported_at):
                raise typer.BadParameter(
                    f"User {user.external_user_id} appears in files from different "
                    "sources or export times",
                    param_hint="export_file",
                )
            for turn in user.turns:
                turns[(turn.role, turn.text, turn.at)] = turn
    return {uid: (first, list(turns.values())) for uid, (first, turns) in merged.items()}


async def import_carry_overs(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    exports: Sequence[CarryOverExport],
) -> Counter[str]:
    """Hold each person's earlier conversation for their next fresh private start.

    Counts `new`, `updated`, `consumed` (already used, never brought back), `empty`, and
    `existing`: the person already has a private conversation here, so their earlier
    one comes with their next /new. They are held all the same.
    """
    counts: Counter[str] = Counter()
    async with sessionmaker.begin() as db:
        tenant = await get_tenant(db, tenant_id)
        if tenant is None:
            raise typer.BadParameter("No such tenant", param_hint="--tenant")
        if tenant.platform != "discord" or any(e.guild_id != tenant.external_id for e in exports):
            raise typer.BadParameter(
                "File is for a different Discord server", param_hint="export_file"
            )
        for user_id, (export, turns) in merge_users(exports).items():
            if await has_conversation(
                db, tenant_id=tenant_id, platform="discord", external_user_id=user_id
            ):
                counts["existing"] += 1
            built = build_carry_over(
                turns, source_name=export.source_name, exported_at=export.exported_at
            )
            if not built.history:
                counts["empty"] += 1
                continue
            counts[
                await put_carry_over(
                    db,
                    tenant_id=tenant_id,
                    platform="discord",
                    external_user_id=user_id,
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
    """Import v1 JSON files of earlier conversations into --tenant, in one transaction.

    Give every file of one export in the same run: a person split over files is merged.
    Their next fresh private conversation (first message or /new) starts from theirs, once.
    """
    exports: list[CarryOverExport] = []
    for path in export_files:
        if path.stat().st_size > MAX_EXPORT_BYTES:
            raise typer.BadParameter(f"{path.name} exceeds 1 MiB")
        exports.append(CarryOverExport.model_validate_json(path.read_bytes()))

    async def run() -> None:
        async with build_runtime(load_settings()) as runtime:
            counts = await import_carry_overs(
                runtime.sessionmaker, tenant_id=tenant, exports=exports
            )
            typer.echo(
                f"{len(exports)} files: {counts['new']} new, {counts['updated']} updated, "
                f"{counts['consumed']} already used, {counts['existing']} already have a "
                f"private conversation (held for their next /new), {counts['empty']} empty"
            )

    asyncio.run(run())
