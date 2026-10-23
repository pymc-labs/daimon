"""daimon agents … sub-app."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, Literal, cast, get_args

import click
import typer
from daimon.adapters.cli.commands.channels import agent_rule_app
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import GUILD_OPTION, JSON_OPTION, TENANT_OPTION, YES_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.prompt import confirm_or_abort
from daimon.adapters.cli.runtime import (
    CLI_AUTHORIZATION_ID,
    FACE_WAIT_S,
    CliRuntime,
    build_runtime,
    cli_principal_id,
    request_resource_host,
)
from daimon.adapters.cli.tenant import (
    TenantSelector,
    discover_tenant,
    resolve_tenant_display,
    resolve_tenant_override,
)
from daimon.core import channel_backend
from daimon.core.agent_fork import CaptureSource, copy_agent, fork_notes
from daimon.core.agent_identity import ensure_agent_face
from daimon.core.agent_lifecycle import archive_memory_store_best_effort, forget_archived_agent
from daimon.core.authz import Subject
from daimon.core.config import load_settings, load_turn_settings
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
    find_agents_by_daimon_tag,
    list_agents_by_tenant,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_READER_OF,
    MA_METADATA_KEY_READER_SOURCE,
    MA_METADATA_KEY_SPEC_HASH,
    build_metadata,
)
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.errors import SpecError, StoreError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import archive_agent, retrieve_agent, update_agent
from daimon.core.specs import load_agent_spec, merge_default_agent_toolset
from daimon.core.stores import agent_tool_rules as tool_rules_store
from daimon.core.stores.agent_google_binding import upsert_agent_google_binding
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.stores.tenants import list_tenants_by_platform
from mux.profiles import selectable_models
from pydantic import BaseModel, TypeAdapter, ValidationError
from rich.console import Console
from typer.core import TyperGroup


class AgentCommands(TyperGroup):
    """Discover optional commands only when the agent group is used."""

    def _prune_disabled(self) -> None:
        if not channel_backend.backend_selection_enabled(load_turn_settings()):
            self.commands.pop("backend", None)

    def list_commands(self, ctx: click.Context) -> list[str]:
        self._prune_disabled()
        return super().list_commands(ctx)

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        self._prune_disabled()
        return super().resolve_command(ctx, args)


agents_app = typer.Typer(
    help="Agents: create, list, get, update, archive, fork, rule.", cls=AgentCommands
)
agents_app.add_typer(agent_rule_app, name="rule")
backend_app = typer.Typer(help="Set or clear an agent's model backend.")
agents_app.add_typer(backend_app, name="backend")
owner_app = typer.Typer(help="Show, set or clear an agent's owner.")
agents_app.add_typer(owner_app, name="owner")
tool_rules_app = typer.Typer(help="List, set or clear an agent's MCP tool rules.")
agents_app.add_typer(tool_rules_app, name="tool-rules")


async def _tool_rule_target(
    rt: CliRuntime, selector: TenantSelector | None, name: str, tool_key: str | None = None
):  # type: ignore[no-untyped-def]
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
        principal = await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    agent = await find_agent_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=name)
    if agent is None:
        raise StoreError(f"no agent named {name!r} in this workspace")
    url = None
    if tool_key is not None:
        server, sep, tool = tool_key.partition("/")
        if not sep or not server or not tool or "/" in tool:
            raise StoreError("tool_key must be exactly server/tool")
        url = next((s.url for s in agent.mcp_servers if s.name == server), None)
        if url is None:
            raise StoreError(f"agent has no MCP server named {server!r}")
    return tenant_id, agent, url, str(principal.id)


@tool_rules_app.command("list")
def tool_rules_list_command(
    ctx: typer.Context,
    agent_name: str,
    as_json: Annotated[bool, JSON_OPTION] = False,
    channel: Annotated[str | None, typer.Option("--channel", help="Parent channel id.")] = None,
) -> None:
    """List one agent's MCP tool rules."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            tenant_id, agent, _, _ = await _tool_rule_target(rt, ctx.obj, agent_name)
            async with rt.sessionmaker() as session:
                rows = await tool_rules_store.list_rules(
                    session, tenant_id=tenant_id, agent_id=agent.id, channel_id=channel
                )
            emit_rows(
                console,
                rows,
                columns=("tool_key", "channel_id", "rule", "scope", "server_url", "revision"),
                as_json=as_json,
            )
            if not rt.settings.tool_safety.agent_rules:
                console.print(
                    "Agent tool rules are inactive until "
                    "DAIMON_TOOL_SAFETY__AGENT_RULES is enabled."
                )

    run_cli(_run(), console=console)


@tool_rules_app.command("set")
def tool_rules_set_command(
    ctx: typer.Context,
    agent_name: str,
    tool_key: str,
    rule: tool_rules_store.Rule,
    scope: tool_rules_store.Scope = "chat",
    channel: Annotated[str | None, typer.Option("--channel", help="Parent channel id.")] = None,
) -> None:
    """Set one agent's rule for exactly server/tool."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            tenant_id, agent, url, actor = await _tool_rule_target(
                rt, ctx.obj, agent_name, tool_key
            )
            assert url is not None
            async with rt.sessionmaker.begin() as session:
                row = await tool_rules_store.set_rule(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent.id,
                    agent_name=agent.name,
                    tool_key=tool_key,
                    channel_id=channel,
                    server_url=url,
                    rule=rule,
                    scope=scope,
                    set_by=actor,
                    actor=Subject(is_admin=True),
                )
            console.print(
                f"{agent_name} {tool_key} channel={channel or 'all'}: "
                f"{row.rule} ({row.scope}), revision {row.revision}"
            )
            if not rt.settings.tool_safety.agent_rules:
                console.print("Inactive until DAIMON_TOOL_SAFETY__AGENT_RULES is enabled.")

    run_cli(_run(), console=console)


@tool_rules_app.command("clear")
def tool_rules_clear_command(
    ctx: typer.Context,
    agent_name: str,
    tool_key: str,
    channel: Annotated[str | None, typer.Option("--channel", help="Parent channel id.")] = None,
) -> None:
    """Clear one agent's rule while preserving its revision history."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            tenant_id, agent, url, actor = await _tool_rule_target(rt, ctx.obj, agent_name)
            async with rt.sessionmaker.begin() as session:
                existing = await tool_rules_store.get_rule(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent.id,
                    tool_key=tool_key,
                    channel_id=channel,
                )
                if existing is not None:
                    url = existing.server_url
                else:
                    server, _, _ = tool_key.partition("/")
                    url = next((s.url for s in agent.mcp_servers if s.name == server), None)
                if url is None:
                    raise StoreError("no stored rule or attached MCP server for that tool")
                row = await tool_rules_store.clear_rule(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent.id,
                    agent_name=agent.name,
                    tool_key=tool_key,
                    channel_id=channel,
                    server_url=url,
                    set_by=actor,
                    actor=Subject(is_admin=True),
                )
            console.print(
                f"{agent_name} {tool_key} channel={channel or 'all'}: cleared, "
                f"revision {row.revision}"
            )

    run_cli(_run(), console=console)


@agents_app.callback()
def agents_callback(
    ctx: typer.Context,
    tenant: Annotated[str | None, TENANT_OPTION] = None,
    guild: Annotated[str | None, GUILD_OPTION] = None,
) -> None:
    ctx.obj = TenantSelector(tenant_id=tenant, guild_id=guild)


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
    ctx: typer.Context,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)
    selector = ctx.obj

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await agents_list(rt=rt, console=console, as_json=as_json, selector=selector)

    run_cli(_with_defaults(), console=console)


async def agents_list(
    *, rt: CliRuntime, console: Console, as_json: bool, selector: TenantSelector | None = None
) -> None:
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
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
    ctx: typer.Context,
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
    selector = ctx.obj

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await agents_get(
                rt=rt,
                console=console,
                name=name,
                as_json=as_json,
                include_archived=include_archived,
                selector=selector,
            )

    run_cli(_with_defaults(), console=console)


async def agents_get(
    *,
    rt: CliRuntime,
    console: Console,
    name: str,
    as_json: bool,
    include_archived: bool = False,
    selector: TenantSelector | None = None,
) -> None:
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    hosted = await _hosted_agent(rt, tenant_id=tenant_id, name=name)
    if hosted is not None:
        emit_rows(
            console, [hosted], columns=("name", "id", "description", "created_at"), as_json=as_json
        )
        return
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


class _HostedAgentRow(BaseModel):
    """An OpenAI agent read back from its provider, as `agents get` reports it."""

    name: str
    id: str
    description: str | None
    created_at: datetime
    model: str
    skills: tuple[str, ...]


async def _hosted_agent(
    rt: CliRuntime, *, tenant_id: uuid.UUID, name: str
) -> _HostedAgentRow | None:
    """The named agent from the OpenAI catalog, read back from the provider, or None.

    Only with the operator capability (`rt.resource_hosts`), and only for a
    tenant the operator's deployment manifest authorizes in the factory's
    workspace: any other tenant keeps the existing path without the catalog
    being opened. Under each authorized account the neutral catalog is read
    fresh, and a host is requested only when an active agent has that name.
    The readback retrieves the provider's agent and refuses one that differs
    from the catalog row.
    """
    from mux.profiles import INLINE_REUSE, PERSISTENT_WORKSPACE

    if rt.gemini_resource_hosts is not None:
        from daimon.adapters.cli.runtime import ResourceHostMismatch, request_gemini_resource_host
        from daimon.core.channel_backend import channel_ref, current_backend

        selected_grants = [g for g in rt.gemini_resource_grants if g.tenant_id == tenant_id]
        for grant in selected_grants:
            async with rt.sessionmaker() as session:
                selected = await current_backend(
                    session, channel_ref(tenant_id, grant.platform, grant.channel_id)
                )
            if selected is None or selected.profile != INLINE_REUSE.profile_id:
                continue
            if selected.digest != grant.config_digest:
                raise ResourceHostMismatch("Gemini resource grant is stale")
            host = request_gemini_resource_host(rt, grant)
            view = await host.find(name)
            if view is None:
                continue
            fresh = await host.read(view.revision.catalog_id)
            spec = fresh.agent.spec
            return _HostedAgentRow(
                name=name,
                id=fresh.agent.ref.id,
                description=spec.description,
                created_at=fresh.agent.created_at,
                model=spec.model.id,
                skills=tuple(skill.id for skill in spec.skills or ()),
            )
    factory = rt.resource_hosts
    if factory is None:
        return None
    from daimon.core.turn.runtimes import authorized_channel_deployments

    accounts = sorted(
        {
            deployment.account_id
            for deployment in authorized_channel_deployments(
                PERSISTENT_WORKSPACE.profile_id, str(tenant_id)
            )
            if deployment.project == factory.account_scope_id
        }
    )
    if not accounts:
        return None
    from daimon.core.stores.neutral_catalog import PostgresNeutralCatalog
    from mux.contracts.ids import Scope

    catalog = PostgresNeutralCatalog(
        rt.sessionmaker, provider="openai", account_scope_id=factory.account_scope_id
    )
    for account in accounts:
        scope = Scope(
            tenant_id=str(tenant_id),
            account_id=account,
            principal_id=cli_principal_id(rt),
            authorization_id=CLI_AUTHORIZATION_ID,
        )
        named = [
            row
            for row in await catalog.list_agents(scope)
            if row.agent.lifecycle == "active"
            and (row.agent.spec.metadata or {}).get(MA_METADATA_KEY_NAME, row.agent.spec.name)
            == name
        ]
        if not named:
            continue
        # Refuses, rather than falls back, if the host is for any other scope.
        host = request_resource_host(
            rt,
            tenant_id=tenant_id,
            account_id=uuid.UUID(account),
            workspace=factory.account_scope_id,
            operation_key=f"cli-agent-get:{uuid.uuid4()}",
        )
        view = await host.find(name)
        if view is None:
            return None
        fresh = await host.read(view.revision.catalog_id)
        spec = fresh.agent.spec
        return _HostedAgentRow(
            name=name,
            id=fresh.agent.ref.id,
            description=spec.description,
            created_at=fresh.agent.created_at,
            model=spec.model.id,
            skills=tuple(skill.id for skill in spec.skills or ()),
        )
    return None


class _GoogleBindingRow(BaseModel):
    """Report row for bind-google output."""

    agent_name: str
    email: str
    scopes: tuple[str, ...]


@agents_app.command("bind-google")
def agents_bind_google_command(
    ctx: typer.Context,
    name: str,
    email: str,
    scopes: Annotated[
        list[str],
        typer.Option("--scopes", help="Google OAuth scope. Repeat for multiple scopes."),
    ],
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)
    selector = ctx.obj

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await agents_bind_google(
                rt=rt,
                console=console,
                name=name,
                email=email,
                scopes=scopes,
                as_json=as_json,
                selector=selector,
            )

    run_cli(_with_defaults(), console=console)


async def agents_bind_google(
    *,
    rt: CliRuntime,
    console: Console,
    name: str,
    email: str,
    scopes: list[str],
    as_json: bool,
    selector: TenantSelector | None = None,
) -> None:
    if len(scopes) == 0:
        raise StoreError("one or more --scopes is required to bind a Google identity.")

    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    agent = await find_agent_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=name)
    if agent is None:
        raise StoreError(f"no agent named {name!r} in your account or system defaults.")

    # The agent_google_binding primary key is agent_id alone (no tenant_id
    # column), so the uuid must be derived tenant-scoped rather than taken
    # from the MA id directly — otherwise one tenant could bind a uuid that
    # belongs to another tenant's agent.
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id))

    async with rt.sessionmaker() as session, session.begin():
        binding = await upsert_agent_google_binding(
            session, agent_id=agent_id, email=email, scopes=scopes
        )

    emit_rows(
        console,
        [_GoogleBindingRow(agent_name=name, email=binding.email, scopes=binding.scopes)],
        columns=("agent_name", "email", "scopes"),
        as_json=as_json,
    )


@agents_app.command("create")
def agents_create_command(
    ctx: typer.Context,
    path: Path,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)
    selector = ctx.obj

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await agents_create(rt=rt, console=console, path=path, selector=selector)

    run_cli(_with_defaults(), console=console)


async def agents_create(
    *, rt: CliRuntime, console: Console, path: Path, selector: TenantSelector | None = None
) -> None:
    spec = load_agent_spec(path)
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    # Tenant-scoped collision pre-check MUST precede reconcile_agent — reconcile
    # silently UPDATEs an existing name instead of raising (name-as-identity).
    # ANY match in this tenant blocks the create regardless of owner.
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
        references=rt.sessionmaker,
    )
    await ensure_agent_face(
        rt.sessionmaker,
        tenant_id=tenant_id,
        agent_name=spec.name,
        metadata=None,  # managed=False above: only the default name can make it built-in
        default_agent_name=rt.deployment_default.agent_name,
        timeout_s=FACE_WAIT_S,
    )
    console.print(f"[green]✓ created agent {spec.name!r}[/green]")


@agents_app.command("update")
def agents_update_command(
    ctx: typer.Context,
    name: str,
    path: Path,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)
    selector = ctx.obj

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await agents_update(rt=rt, console=console, name=name, path=path, selector=selector)

    run_cli(_with_defaults(), console=console)


async def agents_update(
    *,
    rt: CliRuntime,
    console: Console,
    name: str,
    path: Path,
    selector: TenantSelector | None = None,
) -> None:
    spec = load_agent_spec(path)
    if spec.name != name:
        raise SpecError(
            f"cannot rename {name!r} -> {spec.name!r} via update; agent names are identity. "
            "Fork under the new name instead."
        )
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
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
        references=rt.sessionmaker,
    )
    console.print(f"[green]✓ updated agent {spec.name!r}[/green]")


@backend_app.command("set")
def agents_backend_set_command(
    ctx: typer.Context,
    name: str,
    backend: str,
    yes: Annotated[bool, YES_OPTION] = False,
    as_json: Annotated[bool, JSON_OPTION] = False,
    accept_drops: Annotated[str | None, typer.Option("--accept-drops")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    model: Annotated[
        str | None,
        typer.Option("--model", help="One of the backend's selectable models; default if unset."),
    ] = None,
) -> None:
    """Preview and set an enabled backend for new threads using this agent."""
    settings = load_settings()
    console = Console(highlight=False)

    async def work() -> None:
        async with build_runtime(settings) as runtime:
            await agents_backend_change(
                rt=runtime,
                console=console,
                name=name,
                backend=backend,
                yes=yes,
                as_json=as_json,
                selector=ctx.obj,
                accept_drops=accept_drops,
                dry_run=dry_run,
                model=model,
            )

    run_cli(work(), console=console)


@backend_app.command("settings")
def agents_backend_settings_command(
    ctx: typer.Context,
    name: str,
    thread_mode: Annotated[
        str | None,
        typer.Option(
            "--thread-mode", help="per_caller or shared: the agent's own intent for new threads."
        ),
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="A profile of the agent's backend.")
    ] = None,
    require: Annotated[
        list[str] | None,
        typer.Option("--require", help="A capability the agent requires. Repeat for more."),
    ] = None,
    yes: Annotated[bool, YES_OPTION] = False,
    as_json: Annotated[bool, JSON_OPTION] = False,
    accept_drops: Annotated[str | None, typer.Option("--accept-drops")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Preview and state the agent's thread mode, profile and required capabilities.

    Unset options return to the backend's default. A channel that pins a backend
    still overrides the thread mode, and refuses a turn it can't give a
    required capability.
    """
    from mux.contracts.config import CapabilityRequirement, ThreadMode
    from mux.contracts.profile import Capability

    try:
        if thread_mode not in (None, *get_args(ThreadMode)):
            raise ValueError(thread_mode)
        mode = cast(ThreadMode | None, thread_mode)
        needs = TypeAdapter(tuple[Capability, ...]).validate_python(tuple(require or ()))
    except (ValidationError, ValueError) as exc:
        raise typer.BadParameter(
            "--thread-mode is per_caller or shared; --require names a known capability."
        ) from exc
    agent_settings = channel_backend.AgentSettings(
        profile=profile,
        requires={cap: CapabilityRequirement(level="required") for cap in needs} or None,
        thread_mode=mode,
    )
    settings = load_settings()
    console = Console(highlight=False)

    async def work() -> None:
        async with build_runtime(settings) as runtime:
            await agents_backend_change(
                rt=runtime,
                console=console,
                name=name,
                backend=None,
                yes=yes,
                as_json=as_json,
                selector=ctx.obj,
                accept_drops=accept_drops,
                dry_run=dry_run,
                settings=agent_settings,
            )

    run_cli(work(), console=console)


@backend_app.command("clear")
def agents_backend_clear_command(
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, YES_OPTION] = False,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Preview and clear this agent's setting; channel overrides still apply."""
    settings = load_settings()
    console = Console(highlight=False)

    async def work() -> None:
        async with build_runtime(settings) as runtime:
            await agents_backend_change(
                rt=runtime,
                console=console,
                name=name,
                backend=None,
                yes=yes,
                as_json=as_json,
                selector=ctx.obj,
            )

    run_cli(work(), console=console)


@backend_app.command("revert")
def agents_backend_revert_command(
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, YES_OPTION] = False,
    as_json: Annotated[bool, JSON_OPTION] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Preview and restore the preceding retained backend setting."""
    settings = load_settings()
    console = Console(highlight=False)

    async def work() -> None:
        if not channel_backend.backend_selection_enabled(settings.turn):
            raise StoreError("Model backend selection is disabled on this deployment.")
        async with build_runtime(settings) as runtime:
            await agents_backend_change(
                rt=runtime,
                console=console,
                name=name,
                backend=None,
                yes=yes,
                as_json=as_json,
                selector=ctx.obj,
                dry_run=dry_run,
                revert=True,
            )

    run_cli(work(), console=console)


async def agents_backend_change(
    *,
    rt: CliRuntime,
    console: Console,
    name: str,
    backend: str | None,
    yes: bool,
    as_json: bool,
    selector: TenantSelector | None = None,
    accept_drops: str | None = None,
    dry_run: bool = False,
    revert: bool = False,
    model: str | None = None,
    settings: channel_backend.AgentSettings | None = None,
) -> None:
    from mux.contracts.config import BackendConfig

    if not channel_backend.backend_selection_enabled(rt.settings.turn):
        raise StoreError("Model backend selection is disabled on this deployment.")
    try:
        target = BackendConfig.model_validate({"backend": backend}).backend
    except ValidationError as exc:
        raise typer.BadParameter("Choose a backend supported by this deployment.") from exc
    if model is not None:
        if revert or target is None:
            raise typer.BadParameter(
                "--model applies to set with a backend; clear and revert take none."
            )
        if model not in selectable_models(target):
            raise typer.BadParameter(
                f"--model must be one of: {', '.join(selectable_models(target)) or 'none here'}."
            )
    if as_json and not yes and not dry_run:
        raise typer.BadParameter("Use --yes with --json after reviewing the backend preview.")
    async with rt.sessionmaker.begin() as session:
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
        principal = await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    actor = channel_backend.BackendActor(
        tenant_id=tenant_id,
        account_id=principal.account_id,
        subject=Subject(is_admin=True),
    )
    from functools import partial

    from daimon.core.authz import Place
    from daimon.core.scope import ScopeContext
    from daimon.core.turn.admission import capture_backend_source
    from daimon.core.turn.deps import build_turn_deps

    deps = build_turn_deps(
        rt.settings,
        rt.anthropic,
        rt.sessionmaker,
        deployment_default=rt.deployment_default,
        resolver_cache=rt.resolver_cache,
        billing_config=None,
    )
    service = channel_backend.make_backend_service(
        rt.sessionmaker,
        rt.settings.turn,
        capture_source=partial(
            capture_backend_source,
            deps,
            context=ScopeContext(
                tenant_id=tenant_id,
                account_id=principal.account_id,
                platform="cli",
            ),
            subject=actor.subject,
            place=Place(),
            is_dm=False,
            slack_turn_context_id=None,
            private_dm_id=None,
            external_user_id=principal.os_user,
            explicit_target_name=name,
        ),
    )
    agent = await channel_backend.load_backend_agent(
        rt.anthropic, rt.sessionmaker, tenant_id=tenant_id, agent_name=name
    )
    current_revision = await service.current(agent) if revert or settings is not None else None
    preview_settings: channel_backend.AgentSettings | None = None
    if settings is not None:
        # Settings keep the agent's provider and model.
        target = current_revision.backend if current_revision else None
        model = current_revision.model if current_revision else None
        preview_settings = settings
    if revert:
        if current_revision is None:
            raise channel_backend.AgentBackendRefused("no_previous_revision")
        target = None
        model = None
        if current_revision.previous_local is not None:
            previous = await service.revision(agent, current_revision.previous_local)
            if previous is None:
                raise channel_backend.AgentBackendRefused("no_previous_revision")
            # Revert restores the previous model and settings too; its preview must say so.
            target, model = previous.backend, previous.model
            preview_settings = channel_backend.AgentSettings.of(previous)
        else:
            preview_settings = channel_backend.AgentSettings()
    preview = await service.preview(agent, target, model, preview_settings)
    preview_json = TypeAdapter(type(preview)).dump_json(preview).decode()
    if not as_json:
        console.print(
            f"Model backend: {preview.target_backend}"
            + (f" ({preview.target_model})" if preview.target_model else ""),
            markup=False,
        )
        if preview.target_settings:
            console.print(
                "Agent settings: " + json.dumps(preview.target_settings, sort_keys=True),
                markup=False,
            )
        for label, items in (
            ("Unsupported skills", preview.unsupported_skills),
            ("Unsupported MCPs", preview.unsupported_mcps),
            ("Unsupported capabilities", preview.unsupported_capabilities),
            ("Shared-thread limits", preview.shared_thread_limits),
        ):
            console.print(f"{label}: {', '.join(items) or 'None'}", markup=False)
        if preview.declared_drops:
            console.print(
                f"Won't be available on {preview.target_backend}: "
                + ", ".join(preview.declared_drops),
                markup=False,
            )
            console.print("Revert any time; nothing is deleted.", markup=False)
        console.print(preview.cost_line, markup=False)
        console.print("Existing threads keep their backend; channel overrides still apply.")
    if dry_run:
        if as_json:
            console.print(preview_json, markup=False, highlight=False, soft_wrap=True)
        return
    accepted: tuple[str, ...] | None = None
    if not revert and target is not None and preview.drop_consent is not None:
        # A settings change on such a provider names its drops again, as a switch does.
        try:
            consent = type(preview.drop_consent).model_validate(
                {
                    **preview.drop_consent.model_dump(),
                    "declared_drops": tuple(part.strip() for part in accept_drops.split(","))
                    if accept_drops and accept_drops.strip()
                    else (),
                }
            )
        except ValidationError as exc:
            raise typer.BadParameter(
                "--accept-drops must exactly match: " + ",".join(preview.declared_drops)
            ) from exc
        if consent != preview.drop_consent:
            raise typer.BadParameter(
                "--accept-drops must exactly match: " + ",".join(preview.declared_drops)
            )
        accepted = consent.declared_drops
    elif accept_drops is not None:
        raise typer.BadParameter("This preview does not declare drops.")
    confirm_or_abort(
        console,
        f"Change {name}'s {'settings' if settings is not None else 'model backend'}?",
        yes=yes,
    )
    current = await channel_backend.load_backend_agent(
        rt.anthropic, rt.sessionmaker, tenant_id=tenant_id, agent_name=name
    )
    if settings is not None:
        revision = await service.set_settings(
            current,
            settings,
            preview.setting_revision,
            actor,
            expected_preview_digest=preview.digest,
            accepted_drops=accepted,
        )
    elif revert:
        assert current_revision is not None
        revision = await service.revert(
            current,
            actor,
            expected_revision=current_revision.local,
            expected_preview_digest=preview.digest,
        )
    elif target is None:
        revision = await service.clear_backend(
            current,
            preview.setting_revision,
            actor,
            expected_preview_digest=preview.digest,
        )
    else:
        revision = await service.set_backend(
            current,
            target,
            preview.setting_revision,
            actor,
            expected_preview_digest=preview.digest,
            model=preview.target_model,
            accepted_drops=accepted,
        )
    if as_json:
        console.print(
            json.dumps(
                {
                    "agent": name,
                    "backend": revision.backend,
                    "model": revision.model,
                    "revision": revision.local,
                    "preview": json.loads(preview_json),
                }
            ),
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
    else:
        console.print(f"Updated {name}'s model backend for new threads.", markup=False)


@agents_app.command("archive")
def agents_archive_command(
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, YES_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)
    selector = ctx.obj

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await agents_archive(rt=rt, console=console, name=name, yes=yes, selector=selector)

    run_cli(_with_defaults(), console=console)


async def agents_archive(
    *,
    rt: CliRuntime,
    console: Console,
    name: str,
    yes: bool,
    selector: TenantSelector | None = None,
) -> None:
    """Archive an agent, its memory store, its rule and every scope row that names it.

    Same three-step shape as the MCP archive tool and the Slack panel delete:
    archive the MA agent, archive its memory store best-effort, then drop its
    rule and clear the scope rows (`forget_archived_agent`).
    Without the clear, a channel or tenant default naming this agent survives
    the archive and turn resolution misses instead of falling through the
    cascade.
    """
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
        tenant_label = await resolve_tenant_display(session, tenant_id)
        # Confirmation fires AFTER the tenant resolves and BEFORE any write (the
        # principal bootstrap below), so an operator who mistyped --guild sees
        # the resolved tenant named here rather than archiving in the wrong one.
        confirm_or_abort(console, f"archive agent {name!r} in {tenant_label}?", yes=yes)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    agent = await find_agent_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=name)
    if agent is None:
        raise StoreError(f"no agent named {name!r} in your account or system defaults.")
    await archive_agent(rt.anthropic, agent.id, scope=resource_scope(tenant_id=str(tenant_id)))
    await archive_memory_store_best_effort(
        anthropic=rt.anthropic,
        sessionmaker=rt.sessionmaker,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)),
        log_context={"tenant_id": str(tenant_id), "agent_name": name, "ma_agent_id": agent.id},
    )
    # After the MA archive, never before: a failure here leaves an archived
    # agent with a stale rule and scope rows rather than a live agent with cleared ones.
    # Deliberately unguarded — a failure must reach run_cli's error boundary
    # rather than degrade silently.
    await forget_archived_agent(rt.sessionmaker, tenant_id=tenant_id, name=name, agent=agent)

    console.print(f"[green]✓ archived agent {name!r}[/green]")


@agents_app.command(
    "fork",
    help=(
        "Copy an agent under a new name. The copy holds no MCP credentials; the source's "
        "own skills are uploaded again as the copy's, and the skills left off are named."
    ),
)
def agents_fork_command(
    ctx: typer.Context,
    src: str,
    dst: str | None = typer.Argument(default=None),
) -> None:
    settings = load_settings()
    console = Console(highlight=False)
    selector = ctx.obj

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await agents_fork(rt=rt, console=console, src=src, dst=dst, selector=selector)

    run_cli(_with_defaults(), console=console)


async def agents_fork(
    *,
    rt: CliRuntime,
    console: Console,
    src: str,
    dst: str | None,
    selector: TenantSelector | None = None,
) -> None:
    if dst is None:
        raise typer.BadParameter("destination name is required for fork")
    if dst == src:
        raise StoreError(
            f"fork target name {dst!r} conflicts with source; provide a different name."
        )
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
        principal = await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    # Tenant-scoped collision pre-check on the destination name.
    # ANY match in this tenant blocks the fork regardless of owner.
    dst_matches = await find_agents_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=dst)
    if dst_matches:
        raise StoreError(f"agent {dst!r} already exists in this server — pick a different name.")
    source = await find_agent_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=src)
    if source is None:
        raise StoreError(f"no agent named {src!r} in your account or system defaults.")
    public_url = str(rt.settings.mcp.public_url) if rt.settings.mcp.public_url is not None else None
    copy = await copy_agent(
        rt.anthropic,
        rt.sessionmaker,
        tenant_id=tenant_id,
        source=source,
        new_name=dst,
        public_url=public_url,
        # The CLI is the deployment operator.
        subject=Subject(is_admin=True),
        default_agent_name=rt.deployment_default.agent_name,
        # A provider switch that dropped features is carried only from the copy's own capture.
        capture_source=_fork_capture(rt, tenant_id, principal.account_id, principal.os_user, dst),
        # The operator's copy takes everything an admin's does.
        carry="all",
    )
    await ensure_agent_face(
        rt.sessionmaker,
        tenant_id=tenant_id,
        agent_name=dst,
        metadata=copy.agent.metadata,
        default_agent_name=rt.deployment_default.agent_name,
        timeout_s=FACE_WAIT_S,
    )
    console.print(f"[green]✓ forked agent {src!r} → {dst!r}[/green]")
    if copy.copied_skills:
        console.print(f"Copied its own skills: {', '.join(copy.copied_skills)}")
    if copy.dropped_skills:
        left_off = ", ".join(copy.dropped_skills)
        console.print(f"[yellow]Left off the copy: {left_off}[/yellow]")
    for note in fork_notes(copy, dst):
        console.print(note, markup=False)


def _fork_capture(
    rt: CliRuntime, tenant_id: uuid.UUID, account_id: uuid.UUID, os_user: str, name: str
) -> CaptureSource:
    """Capture a new copy the way the provider-switch command captures an agent.

    Built on first use: only a source whose switch dropped features needs it.
    """

    async def capture(agent: channel_backend.BackendAgent) -> channel_backend.BackendAgent:
        from daimon.core.authz import Place
        from daimon.core.scope import ScopeContext
        from daimon.core.turn.admission import capture_backend_source
        from daimon.core.turn.deps import build_turn_deps

        deps = build_turn_deps(
            rt.settings,
            rt.anthropic,
            rt.sessionmaker,
            deployment_default=rt.deployment_default,
            resolver_cache=rt.resolver_cache,
            billing_config=None,
        )
        return await capture_backend_source(
            deps,
            agent,
            context=ScopeContext(tenant_id=tenant_id, account_id=account_id, platform="cli"),
            subject=Subject(is_admin=True),
            place=Place(),
            is_dm=False,
            slack_turn_context_id=None,
            private_dm_id=None,
            external_user_id=os_user,
            explicit_target_name=name,
        )

    return capture


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

    Enumerates every registered Discord and Slack workspace, lists non-archived agents
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
    # Chat platforms only: a Slack tenant's agents need the base toolset just as
    # much as a Discord tenant's. The operator's own `cli` tenant is left alone.
    tenant_rows = await list_tenants_by_platform(rt.sessionmaker, platform="discord")
    tenant_rows += await list_tenants_by_platform(rt.sessionmaker, platform="slack")

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
        fresh = await retrieve_agent(
            rt.anthropic, row.agent_id, scope=resource_scope(tenant_id=row.tenant_id)
        )
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
        await update_agent(
            rt.anthropic,
            fresh.id,
            version=fresh.version,
            payload={"tools": patched_tools},
            scope=resource_scope(tenant_id=row.tenant_id),
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

    Enumerates every registered Discord and Slack workspace, compares each
    agent's current daimon_account against the deterministically-derived guild
    account for that tenant, and updates any agent still pointing at a per-user
    account. Slack stamps the same derived account on create/fork/edit
    (slack/agent_setup/submit.py), so its agents belong in this sweep.

    Operator-tenant agents (no chat workspace) are never enumerated.
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
    # Enumerate registered chat tenants (no raw .list() — ISO-01). Filtering to
    # discord silently left every Slack tenant's agents un-rekeyed; the operator's
    # own `cli` tenant stays excluded, as the docstring promises.
    tenant_rows = await list_tenants_by_platform(rt.sessionmaker, platform="discord")
    tenant_rows += await list_tenants_by_platform(rt.sessionmaker, platform="slack")

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
        agent = await retrieve_agent(
            rt.anthropic, agent_id, scope=resource_scope(tenant_id=str(tid))
        )
        new_meta = build_metadata(
            tenant_id=tid,
            name=new_name,  # renamed if collision; original name otherwise
            account_id=guild_acct,
            managed=(agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"),
            spec_hash=agent.metadata.get(MA_METADATA_KEY_SPEC_HASH),
        )
        # A report reader's link to its source agent is what holds it to the
        # source's pins; a re-key must not drop it.
        for key in (MA_METADATA_KEY_READER_OF, MA_METADATA_KEY_READER_SOURCE):
            if key in agent.metadata:
                new_meta[key] = agent.metadata[key]
        await update_agent(
            rt.anthropic,
            agent.id,
            version=agent.version,
            payload={"name": new_name, "metadata": new_meta},
            scope=resource_scope(tenant_id=str(tid)),
        )

    console.print(f"[green]✓ Re-keyed {n_agents} agent(s).[/green]")
    emit_rows(
        console,
        report_rows,
        columns=("agent_name", "new_name", "agent_id", "new_account"),
        as_json=False,
    )


@owner_app.command("show")
def agents_owner_show_command(ctx: typer.Context, name: str) -> None:
    """Show the agent's owner account, if it has one."""
    settings = load_settings()
    console = Console(highlight=False)

    async def work() -> None:
        async with build_runtime(settings) as runtime:
            await agents_owner(rt=runtime, console=console, name=name, selector=ctx.obj)

    run_cli(work(), console=console)


@owner_app.command("set")
def agents_owner_set_command(
    ctx: typer.Context,
    name: str,
    account: Annotated[
        str | None, typer.Option("--account", help="The owner's account id.")
    ] = None,
    platform: Annotated[
        str | None, typer.Option("--platform", help="With --user: discord, slack or teams.")
    ] = None,
    user: Annotated[
        str | None, typer.Option("--user", help="The owner's platform user id.")
    ] = None,
    yes: Annotated[bool, YES_OPTION] = False,
) -> None:
    """Make one person the agent's owner: they may edit it without being an admin.

    The owner may change its prompt, model, provider, settings, skills, connections
    and fleet; never the provider allowlist, budgets or GitHub access.
    """
    settings = load_settings()
    console = Console(highlight=False)

    async def work() -> None:
        async with build_runtime(settings) as runtime:
            await agents_owner(
                rt=runtime,
                console=console,
                name=name,
                selector=ctx.obj,
                change="set",
                account=account,
                platform=platform,
                user=user,
                yes=yes,
            )

    run_cli(work(), console=console)


@owner_app.command("clear")
def agents_owner_clear_command(
    ctx: typer.Context, name: str, yes: Annotated[bool, YES_OPTION] = False
) -> None:
    """Clear the agent's owner: only admins may edit it."""
    settings = load_settings()
    console = Console(highlight=False)

    async def work() -> None:
        async with build_runtime(settings) as runtime:
            await agents_owner(
                rt=runtime, console=console, name=name, selector=ctx.obj, change="clear", yes=yes
            )

    run_cli(work(), console=console)


async def agents_owner(
    *,
    rt: CliRuntime,
    console: Console,
    name: str,
    selector: TenantSelector | None = None,
    change: Literal["set", "clear"] | None = None,
    account: str | None = None,
    platform: str | None = None,
    user: str | None = None,
    yes: bool = False,
) -> None:
    """The CLI is the operator's own: assigning or clearing an owner is an admin's act."""
    from daimon.core.agent_ownership import agent_owner, assign_agent_owner, clear_agent_owner
    from daimon.core.stores.identity import find_platform_principal

    async with rt.sessionmaker.begin() as session:
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
        principal = await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
        owner_id: uuid.UUID | None = None
        if change == "set":
            if (account is None) == (user is None) or (user is not None and platform is None):
                raise typer.BadParameter("Pass --account, or --platform with --user.")
            if account is not None:
                try:
                    owner_id = uuid.UUID(account)
                except ValueError as exc:
                    raise typer.BadParameter("--account is an account id.") from exc
            else:
                found = await find_platform_principal(
                    session, tenant_id=tenant_id, platform=platform or "", external_id=user or ""
                )
                if found is None:
                    raise StoreError("No account for that platform user in this tenant.")
                owner_id = found.account_id
    agent = await find_agent_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=name)
    if agent is None:
        raise StoreError(f"no agent named {name!r} in your account.")
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id)
    if change is None:
        current = await agent_owner(rt.sessionmaker, tenant_id=tenant_id, agent_id=agent_id)
        console.print(
            f"{name}: owner {current}" if current else f"{name}: no owner (admins only)",
            markup=False,
        )
        return
    if change == "clear":
        confirm_or_abort(console, f"Clear {name}'s owner? Only admins could edit it.", yes=yes)
        cleared = await clear_agent_owner(
            rt.sessionmaker,
            tenant_id=tenant_id,
            agent_id=agent_id,
            by_account_id=principal.account_id,
        )
        console.print(
            f"Cleared {name}'s owner." if cleared else f"{name} had no owner.", markup=False
        )
        return
    assert owner_id is not None
    confirm_or_abort(console, f"Make {owner_id} the owner of {name}?", yes=yes)
    try:
        await assign_agent_owner(
            rt.sessionmaker,
            tenant_id=tenant_id,
            agent_id=agent_id,
            owner_account_id=owner_id,
            by_account_id=principal.account_id,
        )
    except LookupError as exc:
        raise StoreError("That account is not in this tenant.") from exc
    console.print(f"{owner_id} now owns {name}.", markup=False)
