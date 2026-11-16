"""`daimon dm default | access | rule | explain | import-policies`: who may talk to which agent.

Grants say who may DM an agent (everyone, only those listed, or all but those listed).
Rules are ordered; the first that matches a person picks their agent, and a locked one
decides alone. The default agent answers everyone else.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import GUILD_OPTION, JSON_OPTION, TENANT_OPTION
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.adapters.cli.tenant import TenantSelector, discover_tenant, resolve_tenant_override
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import build_subject
from daimon.core.config import load_settings
from daimon.core.dm_access import decide_dm, list_dm_agents
from daimon.core.dm_import import import_dm_policies
from daimon.core.dm_routing import DmChoose, DmRefused, DmStart, DmUnchecked
from daimon.core.errors import StoreError
from daimon.core.stores import dm_access as store
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.direct_messages import dm_enabled, get_dm_agent_name, set_dm_agent_name
from daimon.core.stores.domain import DmGrantMode
from daimon.core.stores.tenants import get_tenant
from rich.console import Console

dm_app = typer.Typer(help="Who may have a private conversation with which agent.")
rule_app = typer.Typer(help="Ordered rules that send a person, a group or everyone to an agent.")
dm_app.add_typer(rule_app, name="rule")

_USER = typer.Option("--user", help="Platform user id; repeat for several.")
_GROUP = typer.Option("--group", help="Role (group) id; repeat for several.")


@dm_app.callback()
def dm_callback(
    ctx: typer.Context,
    tenant: Annotated[str | None, TENANT_OPTION] = None,
    guild: Annotated[str | None, GUILD_OPTION] = None,
) -> None:
    ctx.obj = TenantSelector(tenant_id=tenant, guild_id=guild)


async def _tenant(rt: CliRuntime, selector: TenantSelector | None) -> uuid.UUID:
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        return await discover_tenant(session, override=override)


def _emit(console: Console, data: object, lines: list[str], as_json: bool) -> None:
    if as_json:
        console.print(json.dumps(data), markup=False)
    else:
        for line in lines:
            console.print(line, markup=False)


async def dm_default(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    agent: str | None,
    clear: bool,
    enabled: bool | None = None,
    as_json: bool = False,
) -> None:
    """Show or set the agent that answers people no rule or pick sends elsewhere."""
    if agent is not None and clear:
        raise typer.BadParameter("give an agent or --clear, not both")
    tenant_id = await _tenant(rt, selector)
    async with rt.sessionmaker.begin() as session:
        if agent is not None or clear:
            await set_dm_agent_name(session, tenant_id=tenant_id, agent_name=agent)
        if enabled is not None:
            from daimon.core.stores.direct_messages import set_dm_enabled

            await set_dm_enabled(session, tenant_id=tenant_id, enabled=enabled)
        current = await get_dm_agent_name(session, tenant_id=tenant_id)
        on = await dm_enabled(session, tenant_id=tenant_id)
    _emit(
        console,
        {"enabled": on, "default_agent": current},
        [f"private messages: {'on' if on else 'off'}", f"default agent: {current or '(none)'}"],
        as_json,
    )


async def dm_access(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    agent: str,
    mode: DmGrantMode | None,
    users: list[str],
    groups: list[str],
    as_json: bool = False,
) -> None:
    """Show or replace who may DM one agent."""
    tenant_id = await _tenant(rt, selector)
    if mode is None and (users or groups):
        raise typer.BadParameter("--user and --group need a mode: --mode listed|excluded")
    async with rt.sessionmaker.begin() as session:
        if mode is not None:
            await store.set_grant(
                session,
                tenant_id=tenant_id,
                agent_name=agent,
                who=mode,
                user_ids=users,
                group_ids=groups,
                set_by="cli",
                now=datetime.now(UTC),
            )
        grant = (await store.list_grants(session, tenant_id=tenant_id)).get(agent)
    who = grant.who if grant else "members"
    ids = {
        "users": list(grant.user_ids) if grant else [],
        "groups": list(grant.group_ids) if grant else [],
    }
    _emit(
        console,
        {"agent": agent, "who": who, **ids},
        [
            f"{agent}: {who}",
            f"  users: {', '.join(ids['users']) or '-'}",
            f"  groups: {', '.join(ids['groups']) or '-'}",
        ],
        as_json,
    )


async def dm_rules(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    action: Literal["list", "add", "remove", "move"],
    position: int | None = None,
    to: int | None = None,
    user: str | None = None,
    group: str | None = None,
    everyone: bool = False,
    agent: str | None = None,
    locked: bool = False,
    as_json: bool = False,
) -> None:
    """List the rules, or add, remove or move one."""
    tenant_id = await _tenant(rt, selector)
    now = datetime.now(UTC)
    async with rt.sessionmaker.begin() as session:
        if action == "add":
            chosen = [user is not None, group is not None, everyone]
            if sum(chosen) != 1:
                raise typer.BadParameter("name exactly one of --user, --group, --everyone")
            if agent is None and locked:
                raise typer.BadParameter("a locked rule needs an --agent")
            await store.add_rule(
                session,
                tenant_id=tenant_id,
                subject="user" if user else "group" if group else "everyone",
                subject_id=user or group,
                agent_name=agent,
                locked=locked,
                set_by="cli",
                position=position,
                now=now,
            )
        elif action in ("remove", "move"):
            if position is None or (action == "move" and to is None):
                raise typer.BadParameter("give the rule's position (and --to for a move)")
            done = (
                await store.remove_rule(session, tenant_id=tenant_id, position=position, now=now)
                if action == "remove"
                else await store.move_rule(
                    session, tenant_id=tenant_id, position=position, to=to or 0, now=now
                )
            )
            if not done:
                raise StoreError(f"no rule at position {position}")
        rules = await store.list_rules(session, tenant_id=tenant_id)
    rows = [
        {
            "position": r.position,
            "subject": r.subject,
            "subject_id": r.subject_id,
            "agent": r.agent_name,
            "locked": r.locked,
        }
        for r in rules
    ]
    _emit(
        console,
        rows,
        [
            f"{r['position']}: {r['subject']} {r['subject_id'] or ''} "
            f"-> {r['agent'] or '(default)'}{' (locked)' if r['locked'] else ''}"
            for r in rows
        ]
        or ["no rules"],
        as_json,
    )


async def dm_explain(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    user: str,
    groups: list[str],
    as_json: bool = False,
) -> None:
    """Which agent a person's private message would reach, and why."""
    tenant_id = await _tenant(rt, selector)
    async with rt.sessionmaker() as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None:
            raise StoreError("no such workspace")
        policy = await load_access_policy(session, tenant_id=tenant_id) or TenantAccessPolicy()
        agents = await list_dm_agents(
            session, tenant_id=tenant_id, anthropic=rt.anthropic, records=rt.sessionmaker
        )
        subject = build_subject(
            is_admin=False,
            platform_user_id=user,
            role_ids=tuple(groups),
            verified_membership=True,
        )
        decision = await decide_dm(
            session,
            rt.settings,
            policy,
            tenant_id=tenant_id,
            platform=tenant.platform,
            agents=agents,
            subject=subject,
            account_id=None,
        )
    route = decision.route
    if isinstance(route, DmStart):
        text = f"starts with {route.agent} ({route.reason})"
    elif isinstance(route, DmChoose):
        text = f"chooses among {', '.join(route.agents)}"
    elif isinstance(route, DmRefused):
        text = f"refused ({route.reason})"
    else:
        assert isinstance(route, DmUnchecked)
        text = "group membership unknown"
    rule = decision.rule
    lines = [
        f"{user}: {text}",
        f"  agents that admit them: {', '.join(decision.allowed) or '-'}",
        "  rule: "
        + (
            "none"
            if rule is None
            else f"{rule.subject} {rule.subject_id or ''} -> {rule.agent or '(default)'}"
            f"{' (locked)' if rule.locked else ''}"
        ),
    ]
    _emit(
        console,
        {"user": user, "route": text, "allowed": list(decision.allowed)},
        lines,
        as_json,
    )


async def dm_import_policies(
    *, rt: CliRuntime, console: Console, dry_run: bool, as_json: bool = False
) -> None:
    """Store what the deprecated DAIMON_DIRECT_MESSAGE_POLICIES env setting says."""

    async def agents_of(tenant_id: uuid.UUID):
        async with rt.sessionmaker() as session:
            return await list_dm_agents(
                session, tenant_id=tenant_id, anthropic=rt.anthropic, records=rt.sessionmaker
            )

    async with rt.sessionmaker.begin() as session:
        steps = await import_dm_policies(
            session, rt.settings, list_agents=agents_of, now=datetime.now(UTC), dry_run=dry_run
        )
    _emit(
        console,
        steps,
        [*(("would: " if dry_run else "") + s for s in steps)] or ["nothing to import"],
        as_json,
    )


@dm_app.command("default")
def default_command(
    ctx: typer.Context,
    agent: Annotated[str | None, typer.Argument(help="Agent that answers by default.")] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Remove the default agent.")] = False,
    enabled: Annotated[bool | None, typer.Option("--enabled/--disabled")] = None,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Show or set the default agent for private messages."""
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(load_settings()) as rt:
            await dm_default(
                rt=rt, console=console, selector=ctx.obj, agent=agent, clear=clear,
                enabled=enabled, as_json=as_json,
            )  # fmt: skip

    run_cli(run(), console=console)


@dm_app.command("access")
def access_command(
    ctx: typer.Context,
    agent: str,
    mode: Annotated[str | None, typer.Option("--mode", help="members, listed or excluded.")] = None,
    users: Annotated[list[str] | None, _USER] = None,
    groups: Annotated[list[str] | None, _GROUP] = None,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Show or set who may DM an agent: every member, only those listed, or all but them."""
    if mode is not None and mode not in ("members", "listed", "excluded"):
        raise typer.BadParameter("mode is members, listed or excluded")
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(load_settings()) as rt:
            await dm_access(
                rt=rt, console=console, selector=ctx.obj, agent=agent,
                mode=mode,  # pyright: ignore[reportArgumentType]
                users=users or [], groups=groups or [], as_json=as_json,
            )  # fmt: skip

    run_cli(run(), console=console)


@rule_app.command("list")
def rule_list_command(ctx: typer.Context, as_json: Annotated[bool, JSON_OPTION] = False) -> None:
    """The rules in the order they are tried."""
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(load_settings()) as rt:
            await dm_rules(rt=rt, console=console, selector=ctx.obj, action="list", as_json=as_json)

    run_cli(run(), console=console)


@rule_app.command("add")
def rule_add_command(
    ctx: typer.Context,
    user: Annotated[str | None, typer.Option("--user", help="Platform user id.")] = None,
    group: Annotated[str | None, typer.Option("--group", help="Role (group) id.")] = None,
    everyone: Annotated[bool, typer.Option("--everyone")] = False,
    agent: Annotated[str | None, typer.Option("--agent", help="Default agent when unset.")] = None,
    locked: Annotated[
        bool, typer.Option("--locked", help="Decide alone; picks are ignored.")
    ] = False,
    position: Annotated[
        int | None, typer.Option("--position", help="Insert here; else last.")
    ] = None,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Add a rule for one person, one group or everyone."""
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(load_settings()) as rt:
            await dm_rules(
                rt=rt, console=console, selector=ctx.obj, action="add", user=user, group=group,
                everyone=everyone, agent=agent, locked=locked, position=position, as_json=as_json,
            )  # fmt: skip

    run_cli(run(), console=console)


@rule_app.command("remove")
def rule_remove_command(
    ctx: typer.Context, position: int, as_json: Annotated[bool, JSON_OPTION] = False
) -> None:
    """Remove the rule at a position."""
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(load_settings()) as rt:
            await dm_rules(
                rt=rt, console=console, selector=ctx.obj, action="remove", position=position,
                as_json=as_json,
            )  # fmt: skip

    run_cli(run(), console=console)


@rule_app.command("move")
def rule_move_command(
    ctx: typer.Context, position: int, to: int, as_json: Annotated[bool, JSON_OPTION] = False
) -> None:
    """Move the rule at a position to another."""
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(load_settings()) as rt:
            await dm_rules(
                rt=rt, console=console, selector=ctx.obj, action="move", position=position,
                to=to, as_json=as_json,
            )  # fmt: skip

    run_cli(run(), console=console)


@dm_app.command("explain")
def explain_command(
    ctx: typer.Context,
    user: str,
    groups: Annotated[list[str] | None, _GROUP] = None,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Which agent a person's private message reaches, and why."""
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(load_settings()) as rt:
            await dm_explain(
                rt=rt, console=console, selector=ctx.obj, user=user, groups=groups or [],
                as_json=as_json,
            )  # fmt: skip

    run_cli(run(), console=console)


@dm_app.command("import-policies")
def import_policies_command(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the steps only.")] = False,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Store the deprecated DAIMON_DIRECT_MESSAGE_POLICIES setting as access rules."""
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(load_settings()) as rt:
            await dm_import_policies(rt=rt, console=console, dry_run=dry_run, as_json=as_json)

    run_cli(run(), console=console)
