"""Operator tools for artifact pages: bring in pages exported by another system."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.artifact_pages.importing import Bundle
from daimon.core.config import load_settings
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.stores.artifact_import import ImportTarget, PageReport, import_page
from daimon.core.stores.tenants import get_tenant
from pydantic import ValidationError

artifacts_app = typer.Typer(help="Operator tools for artifact pages.", no_args_is_help=True)

# One page with twenty full versions is about 40 MB of HTML.
_MAX_FILE_BYTES = 64 * 1024 * 1024


def _files(directory: Path) -> list[Path]:
    found = sorted(directory.glob("*.json"))
    if not found:
        raise typer.BadParameter(f"No .json page files in {directory}")
    return found


def _read(path: Path) -> Bundle:
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise typer.BadParameter(f"{path.name} is over {_MAX_FILE_BYTES >> 20} MiB")
    try:
        return Bundle.model_validate_json(path.read_bytes())
    except ValidationError as exc:
        # Locations and messages only: an error never repeats the file's content.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
        )
        raise typer.BadParameter(f"{path.name}: {problems}") from exc


def _line(report: PageReport) -> list[str]:
    lines = [report.slug.ljust(40) + f" {report.action}"]
    if report.action in ("create", "replace"):
        lines[0] += (
            f" members={report.members} visitors={report.visitors} dropped={report.dropped}"
            f" watches={report.watches} orphans_ended={report.orphans_ended}"
        )
    lines += [f"  warning: {w}" for w in report.warnings]
    lines += [f"  refused: {p}" for p in report.problems]
    return lines


@artifacts_app.command("import")
def import_command(
    directory: Annotated[Path, typer.Argument(help="Folder of page files, one JSON file each")],
    tenant: Annotated[uuid.UUID, typer.Option("--tenant", help="The workspace to import into")],
    agent: Annotated[str, typer.Option("--agent", help="Name of the agent the pages belong to")],
    apply: Annotated[
        bool, typer.Option("--apply", help="Write the pages; without it nothing is kept")
    ] = False,
) -> None:
    """Import pages exported as version 1 bundles ("Pages" in the people-migration guide).

    Without --apply, the same work runs in a transaction that is rolled back, and prints
    for each page what would happen and what would not work here. Each page commits on its
    own with --apply. A rerun replaces a page imported from the same address in place and
    refuses any other name or id that is taken. Prints counts, never HTML, data or links.
    """
    # Each file is read once: what was checked here is what is imported.
    bundles = {path: _read(path) for path in _files(directory)}
    for path, bundle in bundles.items():
        if apply and bundle.provisional:
            typer.echo(f"{path.name} is provisional: re-export with --apply first", err=True)
            raise typer.Exit(1)
    slugs = frozenset(b.page.slug for b in bundles.values())
    guilds = {b.guild_id for b in bundles.values()}

    async def run() -> int:
        async with build_runtime(load_settings()) as runtime:
            target = await _target(runtime, tenant, agent, guilds, slugs)
            now = datetime.now(UTC)
            reports: list[PageReport] = []
            if apply:
                for bundle in bundles.values():
                    async with runtime.sessionmaker.begin() as db:
                        reports.append(await import_page(db, bundle.page, target, now=now))
            else:
                async with runtime.sessionmaker() as db, db.begin() as transaction:
                    for bundle in bundles.values():
                        async with db.begin_nested():
                            reports.append(await import_page(db, bundle.page, target, now=now))
                    await transaction.rollback()
        for report in reports:
            for line in _line(report):
                typer.echo(line)
        counts = {k: sum(1 for r in reports if r.action == k) for k in _ACTIONS}
        typer.echo("pages " + " ".join(f"{k}={n}" for k, n in counts.items()))
        if not apply:
            typer.echo("Dry run: nothing was written. Add --apply to import.")
        return 1 if counts["refused"] else 0

    raise typer.Exit(asyncio.run(run()))


_ACTIONS = ("create", "replace", "refused", "expired")


async def _target(
    runtime: CliRuntime,
    tenant_id: uuid.UUID,
    agent_name: str,
    guilds: set[str],
    slugs: frozenset[str],
) -> ImportTarget:
    """The tenant, its agent and the deployment's page rules; each must match the files."""
    async with runtime.sessionmaker() as db:
        tenant = await get_tenant(db, tenant_id)
    if tenant is None or tenant.platform != "discord" or tenant.archived_at is not None:
        raise typer.BadParameter(f"{tenant_id} is not a live Discord server")
    if guilds != {tenant.external_id}:
        raise typer.BadParameter(
            f"The files are for server {', '.join(sorted(guilds))}, not {tenant.external_id}"
        )
    agent = await find_agent_by_daimon_tag(
        runtime.anthropic, tenant_id=tenant_id, name=agent_name, records=runtime.sessionmaker
    )
    if agent is None:
        raise typer.BadParameter(f"No agent {agent_name!r} in this workspace")
    settings = runtime.settings
    web = settings.web
    return ImportTarget(
        tenant_id=tenant_id,
        agent_id=agent.id,
        agent_name=agent_name,
        limits=settings.artifact_pages,
        crypto_keys=tuple(k.get_secret_value() for k in settings.crypto.keys),
        asset_hosts=() if web is None else web.artifact_asset_hosts,
        content_host=web is not None and web.artifact_content_url is not None,
        known_slugs=slugs,
    )
