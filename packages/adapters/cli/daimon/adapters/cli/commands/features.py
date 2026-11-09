"""`daimon features list | get | set | diff`: every feature at a place, over the core service.

The operator acts as an admin with no chat identity, so approvals (a chat admin's call)
are refused here as everywhere else. The agent tier of a `tool.*` writes the agent's own
built-in tool config.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import GUILD_OPTION, JSON_OPTION, TENANT_OPTION
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.adapters.cli.tenant import (
    TenantSelector,
    discover_tenant,
    resolve_tenant_override,
)
from daimon.core.agent_editing import (
    AgentActor,
    AgentEditDeps,
    AgentEditError,
    builtin_reader,
    builtin_writer,
)
from daimon.core.authz import Subject, Surface
from daimon.core.channel_changes import ChannelChangeRefused, Expected
from daimon.core.config import load_settings
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.errors import StaleChannelValue, StoreError
from daimon.core.features.registry import DEFAULT, FEATURES, Tier
from daimon.core.features.service import (
    FeatureActor,
    FeatureView,
    change_feature,
    import_env,
    read_features,
)
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.features.surface import source_text, view_json
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.stores.tenants import get_tenant
from rich.console import Console
from rich.table import Table

features_app = typer.Typer(help="Features at a workspace, agent, channel or thread.")

_AGENT = typer.Option("--agent", help="Agent name: its own settings, and its tools.")
_CHANNEL = typer.Option("--channel", help="Channel id.")
_THREAD = typer.Option("--thread", help="Thread id (needs --channel).")


@features_app.callback()
def features_callback(
    ctx: typer.Context,
    tenant: Annotated[str | None, TENANT_OPTION] = None,
    guild: Annotated[str | None, GUILD_OPTION] = None,
) -> None:
    ctx.obj = TenantSelector(tenant_id=tenant, guild_id=guild)


class _Target:
    """The tenant the command acts on, its platform and the operator's account."""

    def __init__(self, tenant_id: uuid.UUID, platform: str, account_id: uuid.UUID) -> None:
        self.tenant_id = tenant_id
        self.platform = platform
        self.account_id = account_id


async def _target(rt: CliRuntime, selector: TenantSelector | None) -> _Target:
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
        principal = await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
        tenant = await get_tenant(session, tenant_id)
    if tenant is None:
        raise StoreError("no such workspace")
    return _Target(tenant_id, tenant.platform, principal.account_id)


def _edit_deps(rt: CliRuntime) -> AgentEditDeps:
    url = rt.settings.mcp.public_url
    return AgentEditDeps(
        client=rt.anthropic,
        session_factory=rt.sessionmaker,
        public_url=str(url) if url is not None else None,
        denied_tools=rt.settings.tool_safety.denied,
        resolver_cache=rt.resolver_cache,
        deployment_default=rt.deployment_default,
    )


def _agent_actor(target: _Target) -> AgentActor:
    return AgentActor(
        tenant_id=target.tenant_id,
        account_id=target.account_id,
        subject=Subject(is_admin=True),
        surface=Surface.CONFIG,
    )


async def _agent_id(rt: CliRuntime, target: _Target, name: str) -> str:
    agent = await find_agent_by_daimon_tag(rt.anthropic, tenant_id=target.tenant_id, name=name)
    if agent is None:
        raise StoreError(f"no agent named {name!r} in this workspace")
    return agent.id


async def _read(
    rt: CliRuntime,
    target: _Target,
    *,
    agent_id: str | None,
    channel: str | None,
    thread: str | None,
    keys: list[str] | None = None,
) -> tuple[FeatureView, ...]:
    if thread and not channel:
        raise StoreError("--thread needs --channel")
    place = FeaturePlace.of(target.tenant_id, target.platform, channel, thread, agent_id=agent_id)
    builtins = (
        await builtin_reader(_edit_deps(rt), actor=_agent_actor(target), ma_agent_id=agent_id)()
        if agent_id
        else None
    )
    return await read_features(
        rt.sessionmaker, rt.settings, place, keys=keys, agent_builtins=builtins
    )


def _tiers_text(view: FeatureView) -> str:
    return ", ".join(f"{t.tier}={t.text}" for t in view.tiers if t.value != DEFAULT) or "-"


def _emit(
    console: Console, views: tuple[FeatureView, ...], platform: str, *, as_json: bool
) -> None:
    if as_json:
        console.print(
            json.dumps([view_json(v, platform) for v in views]), soft_wrap=True, markup=False
        )
        return
    table = Table(show_header=True, header_style="bold")
    for column in ("key", "in effect", "set at"):
        table.add_column(column)
    for view in views:
        table.add_row(
            view.spec.key, source_text(view.spec, view.resolved, platform), _tiers_text(view)
        )
    console.print(table)


async def features_list(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    agent: str | None,
    channel: str | None,
    thread: str | None,
    keys: list[str] | None = None,
    as_json: bool = False,
) -> None:
    target = await _target(rt, selector)
    agent_id = await _agent_id(rt, target, agent) if agent else None
    views = await _read(rt, target, agent_id=agent_id, channel=channel, thread=thread, keys=keys)
    _emit(console, views, target.platform, as_json=as_json)


def _tier_of(agent: str | None, channel: str | None, thread: str | None, tier: Tier | None) -> Tier:
    if tier is not None:
        return tier
    return "thread" if thread else "channel" if channel else "agent" if agent else "workspace"


async def features_set(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    key: str,
    value: str,
    tier: Tier | None,
    agent: str | None,
    channel: str | None,
    thread: str | None,
    locked: bool = False,
    expected: str | None = None,
) -> None:
    target = await _target(rt, selector)
    agent_id = await _agent_id(rt, target, agent) if agent else None
    if thread and not channel:
        raise StoreError("--thread needs --channel")
    place = FeaturePlace.of(target.tenant_id, target.platform, channel, thread, agent_id=agent_id)
    edit, agent_actor = _edit_deps(rt), _agent_actor(target)
    builtin = agent_id is not None and key.startswith("tool.")

    async def is_admin(_: object) -> bool:
        return True

    actor = FeatureActor(
        target.tenant_id,
        target.account_id,
        "cli",
        None,
        is_admin,
        write_builtin=builtin_writer(edit, actor=agent_actor, ma_agent_id=agent_id)
        if builtin and agent_id
        else None,
        read_builtins=builtin_reader(edit, actor=agent_actor, ma_agent_id=agent_id)
        if builtin and agent_id
        else None,
    )
    try:
        change = await change_feature(
            rt.sessionmaker,
            rt.settings,
            actor,
            key=key,
            tier=_tier_of(agent, channel, thread, tier),
            value=value,
            locked=locked,
            place=place,
            expected=Expected(expected) if expected is not None else None,
        )
    except (ChannelChangeRefused, StaleChannelValue, AgentEditError) as exc:
        raise StoreError(str(exc)) from exc
    console.print(f"{key}: {source_text(change.view.spec, change.view.resolved, target.platform)}")


async def features_diff(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    agent_a: str,
    agent_b: str,
    as_json: bool = False,
) -> None:
    """Each feature where the two agents' own settings, or what is in effect for them, differ."""
    target = await _target(rt, selector)
    sides = [
        await _read(
            rt, target, agent_id=await _agent_id(rt, target, name), channel=None, thread=None
        )
        for name in (agent_a, agent_b)
    ]
    differing = [
        (a, b)
        for a, b in zip(*sides, strict=True)
        if (a.resolved.value, _tiers_text(a)) != (b.resolved.value, _tiers_text(b))
    ]
    if as_json:
        payload: list[dict[str, Any]] = [
            {
                "key": a.spec.key,
                agent_a: view_json(a, target.platform),
                agent_b: view_json(b, target.platform),
            }
            for a, b in differing
        ]
        console.print(json.dumps(payload), soft_wrap=True, markup=False)
        return
    if not differing:
        console.print("The two agents have the same features.")
        return
    table = Table(show_header=True, header_style="bold")
    for column in ("key", agent_a, agent_b):
        table.add_column(column)
    for a, b in differing:
        table.add_row(
            a.spec.key,
            source_text(a.spec, a.resolved, target.platform),
            source_text(b.spec, b.resolved, target.platform),
        )
    console.print(table)


def _check_key(key: str) -> str:
    if key not in FEATURES:
        raise typer.BadParameter(f"unknown feature {key!r}; choose one of: {', '.join(FEATURES)}")
    return key


@features_app.command("list")
def features_list_command(
    ctx: typer.Context,
    agent: Annotated[str | None, _AGENT] = None,
    channel: Annotated[str | None, _CHANNEL] = None,
    thread: Annotated[str | None, _THREAD] = None,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Every feature at a place, what is in effect and where it is set."""
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(load_settings()) as rt:
            await features_list(
                rt=rt,
                console=console,
                selector=ctx.obj,
                agent=agent,
                channel=channel,
                thread=thread,
                as_json=as_json,
            )

    run_cli(run(), console=console)


@features_app.command("get")
def features_get_command(
    ctx: typer.Context,
    key: str,
    agent: Annotated[str | None, _AGENT] = None,
    channel: Annotated[str | None, _CHANNEL] = None,
    thread: Annotated[str | None, _THREAD] = None,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """One feature at a place."""
    _check_key(key)
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(load_settings()) as rt:
            await features_list(
                rt=rt,
                console=console,
                selector=ctx.obj,
                agent=agent,
                channel=channel,
                thread=thread,
                keys=[key],
                as_json=as_json,
            )

    run_cli(run(), console=console)


@features_app.command("set")
def features_set_command(
    ctx: typer.Context,
    key: str,
    value: Annotated[str, typer.Argument(help="A value, or `default` to clear it.")],
    tier: Annotated[
        str | None,
        typer.Option("--tier", help="workspace, agent, channel or thread (from the place)."),
    ] = None,
    agent: Annotated[str | None, _AGENT] = None,
    channel: Annotated[str | None, _CHANNEL] = None,
    thread: Annotated[str | None, _THREAD] = None,
    lock: Annotated[bool, typer.Option("--lock", help="Lower tiers cannot change it.")] = False,
) -> None:
    """Set (or clear) one feature at one tier."""
    _check_key(key)
    if tier is not None and tier not in ("workspace", "agent", "channel", "thread"):
        raise typer.BadParameter("tier is workspace, agent, channel or thread")
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(load_settings()) as rt:
            await features_set(
                rt=rt,
                console=console,
                selector=ctx.obj,
                key=key,
                value=value,
                tier=tier,  # pyright: ignore[reportArgumentType]
                agent=agent,
                channel=channel,
                thread=thread,
                locked=lock,
            )

    run_cli(run(), console=console)


@features_app.command("diff")
def features_diff_command(
    ctx: typer.Context,
    agent_a: str,
    agent_b: str,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """The features two agents differ on."""
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(load_settings()) as rt:
            await features_diff(
                rt=rt,
                console=console,
                selector=ctx.obj,
                agent_a=agent_a,
                agent_b=agent_b,
                as_json=as_json,
            )

    run_cli(run(), console=console)


@features_app.command("import-env")
def features_import_env_command() -> None:
    """Copy the deprecated per-workspace env maps into workspace settings, once.

    Until a workspace is imported its env map still applies; afterwards the variable is
    ignored and logged. Safe to re-run: imported workspaces and stored values are kept.
    """
    console = Console(highlight=False)

    async def run() -> None:
        settings = load_settings()
        async with build_runtime(settings) as rt, rt.sessionmaker() as session, session.begin():
            count = await import_env(session, settings)
        console.print(f"imported {count} workspace(s)")

    run_cli(run(), console=console)
