"""daimon agents … sub-app."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Final, cast

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION, YES_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.prompt import confirm_or_abort
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.adapters.cli.tenant import discover_tenant
from daimon.core.config import load_settings
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
    find_agents_by_daimon_tag,
    list_agents_by_tenant,
)
from daimon.core.defaults.mcp_merge import merge_default_mcp_server, merge_default_mcp_toolset
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_SPEC_HASH,
    build_metadata,
)
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.errors import SpecError, StoreError
from daimon.core.specs import load_agent_spec, merge_default_agent_toolset
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.stores.tenants import list_tenants_by_platform
from pydantic import BaseModel
from rich.console import Console

agents_app = typer.Typer(help="Agents: create, list, get, update, archive, fork.")

_CREATE_FIELDS: Final = frozenset(
    {
        "name",
        "model",
        "description",
        "system",
        "tools",
        "mcp_servers",
        "skills",
        "metadata",
    }
)


@agents_app.command("list")
def agents_list_command(
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await agents_list(rt=rt, console=console, as_json=as_json)

    run_cli(_with_defaults(), console=console)


async def agents_list(*, rt: CliRuntime, console: Console, as_json: bool) -> None:
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    rows = await list_agents_by_tenant(rt.anthropic, tenant_id=tenant_id)
    emit_rows(
        console,
        rows,
        columns=("name", "id", "description", "created_at"),
        as_json=as_json,
    )


@agents_app.command("get")
def agents_get_command(
    name: str,
    as_json: Annotated[bool, JSON_OPTION] = False,
    include_archived: Annotated[
        bool,
        typer.Option(
            "--include-archived",
            help="Include archived agents when looking up by name.",
        ),
    ] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await agents_get(
                rt=rt,
                console=console,
                name=name,
                as_json=as_json,
                include_archived=include_archived,
            )

    run_cli(_with_defaults(), console=console)


async def agents_get(
    *,
    rt: CliRuntime,
    console: Console,
    name: str,
    as_json: bool,
    include_archived: bool = False,
) -> None:
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    agent = await find_agent_by_daimon_tag(
        rt.anthropic, tenant_id=tenant_id, name=name, include_archived=include_archived
    )
    if agent is None:
        raise StoreError(f"no agent named {name!r} in your account or system defaults.")
    emit_rows(
        console,
        [agent],
        columns=("name", "id", "description", "created_at"),
        as_json=as_json,
    )


@agents_app.command("create")
def agents_create_command(
    path: Path,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await agents_create(rt=rt, console=console, path=path)

    run_cli(_with_defaults(), console=console)


async def agents_create(*, rt: CliRuntime, console: Console, path: Path) -> None:
    spec = load_agent_spec(path)
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    # Tenant-scoped collision pre-check MUST precede reconcile_agent — reconcile
    # silently UPDATEs an existing name instead of raising (name-as-identity).
    # ANY match in this tenant blocks the create regardless of owner (D-72-01, #136).
    matches = await find_agents_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=spec.name)
    if matches:
        raise StoreError(
            f"agent {spec.name!r} already exists in this server — pick a different name, "
            "or use 'daimon agents update' to modify it."
        )
    public_url = str(rt.settings.mcp.public_url) if rt.settings.mcp.public_url is not None else None
    # Route through reconcile_agent so the daimon-mcp server + its mcp_toolset are
    # merged into the payload, credential guidance is applied, and spec_hash is
    # stamped. managed=False keeps user-owned agents out of the defaults-apply sweep.
    # account_id: use the guild account so the panel can edit CLI-created agents.
    await reconcile_agent(
        rt.anthropic,
        spec,
        tenant_id=tenant_id,
        dry_run=False,
        account_id=derive_guild_account_uuid(tenant_id),
        public_url=public_url,
        managed=False,
    )
    console.print(f"[green]✓ created agent {spec.name!r}[/green]")


@agents_app.command("update")
def agents_update_command(
    name: str,
    path: Path,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await agents_update(rt=rt, console=console, name=name, path=path)

    run_cli(_with_defaults(), console=console)


async def agents_update(
    *,
    rt: CliRuntime,
    console: Console,
    name: str,
    path: Path,
) -> None:
    spec = load_agent_spec(path)
    if spec.name != name:
        raise SpecError(
            f"cannot rename {name!r} -> {spec.name!r} via update; agent names are identity. "
            "Fork under the new name instead."
        )
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    agent = await find_agent_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=name)
    if agent is None:
        raise StoreError(f"no agent named {name!r} in your account.")
    public_url = str(rt.settings.mcp.public_url) if rt.settings.mcp.public_url is not None else None
    # Route through reconcile_agent so the daimon-mcp server + its mcp_toolset are
    # re-merged into the payload. MA's update is a per-field partial merge: a raw
    # `tools` array would drop the inherited mcp_toolset while preserving the
    # mcp_servers entry, leaving a server with no toolset referencing it -> 400.
    # managed=False keeps user-owned agents out of the defaults-apply sweep.
    # account_id: reconcile rebuilds the full metadata dict on every update, so
    # the guild account must be re-stamped here — passing the personal CLI
    # principal would silently flip guild-owned agents to personal ownership.
    await reconcile_agent(
        rt.anthropic,
        spec,
        tenant_id=tenant_id,
        dry_run=False,
        account_id=derive_guild_account_uuid(tenant_id),
        public_url=public_url,
        managed=False,
    )
    console.print(f"[green]✓ updated agent {spec.name!r}[/green]")


@agents_app.command("archive")
def agents_archive_command(
    name: str,
    yes: Annotated[bool, YES_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await agents_archive(rt=rt, console=console, name=name, yes=yes)

    run_cli(_with_defaults(), console=console)


async def agents_archive(*, rt: CliRuntime, console: Console, name: str, yes: bool) -> None:
    confirm_or_abort(console, f"archive agent {name!r}?", yes=yes)
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    agent = await find_agent_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=name)
    if agent is None:
        raise StoreError(f"no agent named {name!r} in your account or system defaults.")
    await rt.anthropic.beta.agents.archive(agent.id)
    console.print(f"[green]✓ archived agent {name!r}[/green]")


@agents_app.command(
    "fork",
    help=("Create a new MA agent seeded from the source's content and a local row pointing at it."),
)
def agents_fork_command(
    src: str,
    dst: str | None = typer.Argument(default=None),
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await agents_fork(rt=rt, console=console, src=src, dst=dst)

    run_cli(_with_defaults(), console=console)


async def agents_fork(
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
    # Tenant-scoped collision pre-check on the destination name (D-72-01, #136).
    # ANY match in this tenant blocks the fork regardless of owner.
    dst_matches = await find_agents_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=dst)
    if dst_matches:
        raise StoreError(f"agent {dst!r} already exists in this server — pick a different name.")
    source = await find_agent_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=src)
    if source is None:
        raise StoreError(f"no agent named {src!r} in your account or system defaults.")
    source_ma = await rt.anthropic.beta.agents.retrieve(source.id)
    params = source_ma.model_dump(mode="json")
    fork_params = {k: params[k] for k in _CREATE_FIELDS if k in params}
    fork_params["name"] = dst
    fork_params["metadata"] = build_metadata(
        tenant_id=tenant_id,
        name=dst,
        account_id=derive_guild_account_uuid(tenant_id),
    )
    public_url = str(rt.settings.mcp.public_url) if rt.settings.mcp.public_url is not None else None
    # Add daimon-mcp server + toolset BOTH halves — MA validates that every
    # server in mcp_servers is referenced by some mcp_toolset tool (400 otherwise).
    fork_params["mcp_servers"] = merge_default_mcp_server(
        fork_params.get("mcp_servers"),  # type: ignore[arg-type]
        public_url,
    )
    fork_params["tools"] = merge_default_mcp_toolset(
        fork_params.get("tools"),  # type: ignore[arg-type]
        public_url,
    )
    # Fork copies raw MA state and bypasses dump_agent_spec — guarantee the
    # base toolset here so forking a legacy pre-guarantee agent doesn't
    # propagate the skills-unusable hole.
    fork_params["tools"] = merge_default_agent_toolset(
        fork_params.get("tools"),  # type: ignore[arg-type]
    )
    await rt.anthropic.beta.agents.create(**fork_params)  # type: ignore[arg-type]
    console.print(f"[green]✓ forked agent {src!r} → {dst!r}[/green]")


class _BackfillRow(BaseModel):
    """Report row for backfill-toolset output."""

    tenant_id: str
    agent_id: str
    agent_name: str
    tool_count: int


@agents_app.command("backfill-toolset")
def agents_backfill_toolset_command(
    yes: Annotated[bool, YES_OPTION] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="List agents that would be patched without writing."),
    ] = False,
) -> None:
    """Patch every tenant-tagged agent missing the base agent_toolset_20260401.

    Enumerates every registered discord workspace, lists non-archived agents
    per tenant, and adds the base agent toolset to any agent that lacks it.
    Agents that already carry the toolset are skipped. Re-running selects zero
    agents (idempotent by construction).
    """
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await agents_backfill_toolset(rt=rt, console=console, yes=yes, dry_run=dry_run)

    run_cli(_with_defaults(), console=console)


async def agents_backfill_toolset(
    *,
    rt: CliRuntime,
    console: Console,
    yes: bool,
    dry_run: bool,
) -> None:
    """Async implementation of backfill-toolset."""
    tenant_rows = await list_tenants_by_platform(rt.sessionmaker, platform="discord")

    report_rows: list[_BackfillRow] = []
    for tenant_row in tenant_rows:
        tenant_id: uuid.UUID = tenant_row.id
        agents = await list_agents_by_tenant(rt.anthropic, tenant_id=tenant_id)
        for agent in agents:
            has_toolset = any(tool.type == "agent_toolset_20260401" for tool in agent.tools)
            if not has_toolset:
                report_rows.append(
                    _BackfillRow(
                        tenant_id=str(tenant_id),
                        agent_id=agent.id,
                        agent_name=agent.metadata.get(MA_METADATA_KEY_NAME, agent.name),
                        tool_count=len(agent.tools),
                    )
                )

    tenant_count = len({row.tenant_id for row in report_rows})
    n_agents = len(report_rows)

    if not report_rows:
        console.print("[green]✓ No agents need the base agent toolset.[/green]")
        return

    summary = f"{n_agents} agent(s) lack the base agent toolset across {tenant_count} tenant(s)"

    if dry_run:
        console.print(f"[yellow]dry-run:[/yellow] {summary}")
        emit_rows(
            console,
            report_rows,
            columns=("tenant_id", "agent_id", "agent_name", "tool_count"),
            as_json=False,
        )
        return

    confirm_or_abort(console, summary, yes=yes)

    for row in report_rows:
        fresh = await rt.anthropic.beta.agents.retrieve(row.agent_id)
        # Re-check predicate against fresh copy — a concurrent edit may have
        # already added the toolset; skip rather than double-apply.
        already_has_toolset = any(tool.type == "agent_toolset_20260401" for tool in fresh.tools)
        if already_has_toolset:
            console.print(
                f"[yellow]skipped[/yellow] {row.agent_name!r} (toolset added concurrently)"
            )
            continue
        patched_tools = merge_default_agent_toolset(
            [t.model_dump(mode="json", exclude_none=True) for t in fresh.tools]  # type: ignore[arg-type]
        )
        await rt.anthropic.beta.agents.update(
            fresh.id,
            version=fresh.version,
            tools=patched_tools,  # type: ignore[arg-type]
        )
        console.print(f"[green]✓[/green] patched {row.agent_name!r}")

    console.print(f"[green]✓ Patched {n_agents} agent(s).[/green]")


class _RekeyRow(BaseModel):
    """Report row for rekey-guild-ownership output."""

    agent_id: str
    agent_name: str
    new_name: str  # equals agent_name unless a collision rename is required
    tenant_id: str
    current_account: str
    new_account: str


@agents_app.command("rekey-guild-ownership")
def agents_rekey_command(
    yes: Annotated[bool, YES_OPTION] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="List what would be re-keyed without writing."),
    ] = False,
) -> None:
    """Re-key existing guild-tenant agents' daimon_account to the derived guild account.

    Enumerates every registered discord workspace, compares each agent's current
    daimon_account against the deterministically-derived guild account for that
    tenant, and updates any agent still pointing at a per-user account.

    Pre-48 operator-tenant agents (no discord workspace) are never enumerated.
    Already-guild-owned and system agents (no daimon_account) are skipped.
    """
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await agents_rekey(rt=rt, console=console, yes=yes, dry_run=dry_run)

    run_cli(_with_defaults(), console=console)


async def agents_rekey(
    *,
    rt: CliRuntime,
    console: Console,
    yes: bool,
    dry_run: bool,
) -> None:
    """Async implementation of rekey-guild-ownership."""
    # Enumerate all registered discord tenants (no raw .list() — ISO-01).
    tenant_rows = await list_tenants_by_platform(rt.sessionmaker, platform="discord")

    # Collect items that need re-keying — (tenant_id, guild_account, agent_id,
    # name, current_acct) — plus the names already claimed by guild-owned agents
    # per tenant. Both come from ONE agent snapshot per tenant so the collision
    # math never operates on inconsistent data.
    to_rekey: list[tuple[uuid.UUID, uuid.UUID, str, str, str]] = []
    guild_owned_names: dict[uuid.UUID, set[str]] = {}

    for tenant_row in tenant_rows:
        tenant_id: uuid.UUID = tenant_row.id
        guild_account: uuid.UUID = derive_guild_account_uuid(tenant_id)

        agents = await list_agents_by_tenant(rt.anthropic, tenant_id=tenant_id)
        # Canonical-first: max created_at keeps the bare name on collision
        # (list_agents_by_tenant returns MA list order, which is unsorted).
        agents.sort(key=lambda agent: agent.created_at, reverse=True)
        guild_owned_names[tenant_id] = set()
        for agent in agents:
            current_account = agent.metadata.get(MA_METADATA_KEY_ACCOUNT)
            agent_name = agent.metadata.get(MA_METADATA_KEY_NAME, agent.name)
            # Skip system agents (no daimon_account) and already-guild-owned.
            if current_account is None:
                continue
            if current_account == str(guild_account):
                guild_owned_names[tenant_id].add(agent_name)
                continue
            to_rekey.append((tenant_id, guild_account, agent.id, agent_name, current_account))

    if not to_rekey:
        console.print("[green]✓ No agents need re-keying.[/green]")
        return

    # -----------------------------------------------------------------------
    # Collision-rename: post-rekey, every (tenant, name) pair must be unique
    # among guild-stamped agents — including names held by already-guild-owned
    # agents that are skipped above. The canonical member of each colliding
    # group (max created_at — to_rekey is built from a created_at-descending
    # sort) keeps the bare name; the others get the smallest free numeric
    # suffix (-2, -3, …).
    # -----------------------------------------------------------------------

    # Determine the post-rekey name for each candidate in two passes so the
    # final name set is collision-free against BOTH already-guild-owned names
    # and every other candidate's assigned name:
    #   pass 1 — a candidate whose bare name is still free (not guild-owned,
    #            not claimed by an earlier candidate) keeps it;
    #   pass 2 — everything else gets the smallest numeric suffix (-2, -3, …)
    #            not taken in the complete per-tenant claimed set.
    claimed_names: dict[uuid.UUID, set[str]] = {
        tid: set(names) for tid, names in guild_owned_names.items()
    }
    resolved_names: dict[int, str] = {}  # to_rekey index → post-rekey name

    for i, (tid, _guild_acct, _agent_id, agent_name, _current_acct) in enumerate(to_rekey):
        taken = claimed_names.setdefault(tid, set())
        if agent_name not in taken:
            taken.add(agent_name)
            resolved_names[i] = agent_name

    for i, (tid, _guild_acct, _agent_id, agent_name, _current_acct) in enumerate(to_rekey):
        if i in resolved_names:
            continue
        taken = claimed_names[tid]
        suffix = 2
        while f"{agent_name}-{suffix}" in taken:
            suffix += 1
        new_name = f"{agent_name}-{suffix}"
        taken.add(new_name)
        resolved_names[i] = new_name

    post_rekey_names: list[str] = [resolved_names[i] for i in range(len(to_rekey))]

    n_agents = len(to_rekey)
    guild_count = len({item[0] for item in to_rekey})
    rename_count = sum(
        1 for orig, new in zip(to_rekey, post_rekey_names, strict=True) if orig[3] != new
    )
    summary = (
        f"{n_agents} agent(s) across {guild_count} guild(s) will be re-keyed to guild ownership"
        + (f" ({rename_count} collision rename(s))" if rename_count else "")
    )

    # Build report rows for display.
    report_rows = [
        _RekeyRow(
            agent_id=agent_id,
            agent_name=agent_name,
            new_name=new_name,
            tenant_id=str(tid),
            current_account=current_acct,
            new_account=str(guild_acct),
        )
        for (tid, guild_acct, agent_id, agent_name, current_acct), new_name in zip(
            to_rekey, post_rekey_names, strict=True
        )
    ]

    if dry_run:
        console.print(f"[yellow]dry-run:[/yellow] {summary}")
        emit_rows(
            console,
            report_rows,
            columns=("agent_name", "new_name", "agent_id", "current_account", "new_account"),
            as_json=False,
        )
        return

    confirm_or_abort(console, summary, yes=yes)

    # Retrieve each agent fresh to get the current version before writing.
    for (tid, guild_acct, agent_id, _agent_name, _current_acct), new_name in zip(
        to_rekey, post_rekey_names, strict=True
    ):
        agent = await rt.anthropic.beta.agents.retrieve(agent_id)
        new_meta = build_metadata(
            tenant_id=tid,
            name=new_name,  # renamed if collision; original name otherwise
            account_id=guild_acct,
            managed=(agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"),
            spec_hash=agent.metadata.get(MA_METADATA_KEY_SPEC_HASH),
        )
        await rt.anthropic.beta.agents.update(
            agent.id,
            version=agent.version,
            name=new_name,
            metadata=cast("dict[str, str | None]", new_meta),
        )

    console.print(f"[green]✓ Re-keyed {n_agents} agent(s).[/green]")
    emit_rows(
        console,
        report_rows,
        columns=("agent_name", "new_name", "agent_id", "new_account"),
        as_json=False,
    )
