"""daimon environments … sub-app."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

import typer
from anthropic import APIStatusError
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION, YES_OPTION
from daimon.adapters.cli.mux_compat import (
    create_environment_ignored,
    delete_environment,
    retrieve_environment,
)
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.prompt import confirm_or_abort
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.adapters.cli.tenant import discover_tenant
from daimon.core.config import load_settings
from daimon.core.defaults.ma_index import (
    find_environment_by_daimon_tag,
    find_environments_by_daimon_tag,
    list_environments_by_tenant,
)
from daimon.core.defaults.metadata import build_metadata
from daimon.core.errors import StoreError
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import archive_environment, create_environment, update_environment
from daimon.core.specs import load_environment_spec
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.stores.scoped_config_write import clear_environment_references
from rich.console import Console

environments_app = typer.Typer(
    help="Environments: create, list, get, update, archive, delete, fork."
)


@environments_app.command("list")
def environments_list_command(
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await environments_list(rt=rt, console=console, as_json=as_json)

    run_cli(_with_defaults(), console=console)


async def environments_list(*, rt: CliRuntime, console: Console, as_json: bool) -> None:
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    rows = await list_environments_by_tenant(rt.anthropic, tenant_id=tenant_id)
    emit_rows(
        console,
        rows,
        columns=("name", "id", "description", "created_at"),
        as_json=as_json,
    )


@environments_app.command("get")
def environments_get_command(
    name: str,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await environments_get(rt=rt, console=console, name=name, as_json=as_json)

    run_cli(_with_defaults(), console=console)


async def environments_get(*, rt: CliRuntime, console: Console, name: str, as_json: bool) -> None:
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    env = await find_environment_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=name)
    if env is None:
        raise StoreError(f"no environment named {name!r} in your account or system defaults.")
    emit_rows(
        console,
        [env],
        columns=("name", "id", "description", "created_at"),
        as_json=as_json,
    )


@environments_app.command("create")
def environments_create_command(
    path: Path,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await environments_create(rt=rt, console=console, path=path)

    run_cli(_with_defaults(), console=console)


async def environments_create(*, rt: CliRuntime, console: Console, path: Path) -> None:
    spec = load_environment_spec(path)
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    # Tenant-scoped collision pre-check before the MA create — a same-name create
    # would otherwise feed reconcile dedup. ANY match blocks regardless of owner.
    matches = await find_environments_by_daimon_tag(
        rt.anthropic, tenant_id=tenant_id, name=spec.name
    )
    if matches:
        raise StoreError(
            f"environment {spec.name!r} already exists in this server — pick a different name, "
            "or use 'daimon environments update' to modify it."
        )
    await create_environment(
        rt.anthropic,
        {
            **spec.model_dump(exclude_none=True),
            "metadata": build_metadata(tenant_id=tenant_id, name=spec.name),
        },
        scope=resource_scope(tenant_id=str(tenant_id)),
    )
    console.print(f"[green]✓ created environment {spec.name!r}[/green]")


@environments_app.command("update")
def environments_update_command(
    name: str,
    path: Path,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await environments_update(rt=rt, console=console, name=name, path=path)

    run_cli(_with_defaults(), console=console)


async def environments_update(*, rt: CliRuntime, console: Console, name: str, path: Path) -> None:
    spec = load_environment_spec(path)
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    env = await find_environment_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=name)
    if env is None:
        raise StoreError(f"no environment named {name!r} in your account.")
    if spec.name != name:
        console.print(f"[yellow]note: renaming environment {name!r} -> {spec.name!r}[/yellow]")
    await update_environment(
        rt.anthropic,
        env.id,
        {
            **spec.model_dump(exclude_none=True),
            "metadata": build_metadata(tenant_id=tenant_id, name=spec.name),
        },
        scope=resource_scope(tenant_id=str(tenant_id)),
    )
    console.print(f"[green]✓ updated environment {spec.name!r}[/green]")


@environments_app.command("archive")
def environments_archive_command(
    name: str,
    yes: Annotated[bool, YES_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await environments_archive(rt=rt, console=console, name=name, yes=yes)

    run_cli(_with_defaults(), console=console)


async def environments_archive(*, rt: CliRuntime, console: Console, name: str, yes: bool) -> None:
    confirm_or_abort(console, f"archive environment {name!r}?", yes=yes)
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    env = await find_environment_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=name)
    if env is None:
        raise StoreError(f"no environment named {name!r} in your account or system defaults.")
    await archive_environment(rt.anthropic, env.id, scope=resource_scope(tenant_id=str(tenant_id)))
    await _clear_picks(rt, console, tenant_id=tenant_id, name=name)
    console.print(f"[green]✓ archived environment {name!r}[/green]")


async def _clear_picks(
    rt: CliRuntime, console: Console, *, tenant_id: uuid.UUID, name: str
) -> None:
    """Clear channel and tenant picks of a removed environment so they fall through."""
    async with rt.sessionmaker() as session, session.begin():
        cleared = await clear_environment_references(
            session, tenant_id=tenant_id, environment_name=name
        )
    if cleared:
        picks = "pick" if cleared == 1 else "picks"
        console.print(f"cleared {cleared} channel or tenant {picks} of {name!r}")


@environments_app.command("delete")
def environments_delete_command(
    name: str,
    yes: Annotated[bool, YES_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await environments_delete(rt=rt, console=console, name=name, yes=yes)

    run_cli(_with_defaults(), console=console)


async def environments_delete(*, rt: CliRuntime, console: Console, name: str, yes: bool) -> None:
    confirm_or_abort(console, f"delete environment {name!r}?", yes=yes)
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    env = await find_environment_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=name)
    if env is None:
        raise StoreError(f"no environment named {name!r} in your account or system defaults.")
    archived_instead = False
    try:
        await delete_environment(
            rt.anthropic, env.id, scope=resource_scope(tenant_id=str(tenant_id))
        )
    except APIStatusError as err:
        if err.status_code == 409:
            await archive_environment(
                rt.anthropic, env.id, scope=resource_scope(tenant_id=str(tenant_id))
            )
            archived_instead = True
        else:
            raise
    await _clear_picks(rt, console, tenant_id=tenant_id, name=name)
    if archived_instead:
        console.print(
            f"[green]✓ archived environment {name!r} (delete blocked: environment in use)[/green]"
        )
    else:
        console.print(f"[green]✓ deleted environment {name!r}[/green]")


@environments_app.command(
    "fork",
    help=(
        "Create a new MA environment seeded from the source's content and a "
        "local row pointing at it."
    ),
)
def environments_fork_command(
    src: str,
    dst: str | None = typer.Argument(default=None),
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await environments_fork(rt=rt, console=console, src=src, dst=dst)

    run_cli(_with_defaults(), console=console)


async def environments_fork(
    *,
    rt: CliRuntime,
    console: Console,
    src: str,
    dst: str | None,
) -> None:
    if dst is None:
        raise typer.BadParameter("destination name is required for fork")
    if dst == src:
        raise StoreError(
            f"fork target name {dst!r} conflicts with source; provide a different name."
        )
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    # The dst==src guard above only catches the trivial case; a fork to an existing
    # *different* environment name would silently collide and feed reconcile dedup.
    # Reject ANY tenant match on dst regardless of owner, before the MA create.
    dst_matches = await find_environments_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=dst)
    if dst_matches:
        raise StoreError(
            f"environment {dst!r} already exists in this server — pick a different name, "
            "or use 'daimon environments update' to modify it."
        )
    source = await find_environment_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=src)
    if source is None:
        raise StoreError(f"no environment named {src!r} in your account or system defaults.")
    source_ma = await retrieve_environment(
        rt.anthropic, source.id, scope=resource_scope(tenant_id=str(tenant_id))
    )
    source_cfg = source_ma.config.model_dump(mode="json")
    allowed = ("type", "networking", "packages")
    fork_cfg = {k: source_cfg[k] for k in allowed if k in source_cfg}
    await create_environment_ignored(
        rt.anthropic,
        {
            "name": dst,
            "config": fork_cfg,
            "description": source_ma.description,
            "metadata": build_metadata(tenant_id=tenant_id, name=dst),
        },
        scope=resource_scope(tenant_id=str(tenant_id)),
    )
    console.print(f"[green]✓ forked environment {src!r} → {dst!r}[/green]")
