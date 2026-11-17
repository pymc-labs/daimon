"""`daimon support limit | menu | channel | requests`: Ask a person's credits, menus and channel.

The operator acts as an admin over the audited change service (`SET_SUPPORT`),
the same rule the MCP tools and the web follow. Each write appends a security event
in the change's own transaction. Limits and menus are read per command, never cached.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
import yaml
from daimon.adapters.cli.channel_lookup import parent_channel
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import GUILD_OPTION, JSON_OPTION, TENANT_OPTION
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.adapters.cli.tenant import TenantSelector, discover_tenant, resolve_tenant_override
from daimon.core.authz import Action, Subject
from daimon.core.channel_changes import ChannelActor, Expected
from daimon.core.config import load_settings
from daimon.core.defaults.ma_index import agent_id_in_tenant
from daimon.core.errors import StaleChannelValue, StoreError
from daimon.core.stores import support as store
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.stores.security_audit import append_event
from daimon.core.stores.support_rows import SupportEscalationRow, SupportLimitRow
from daimon.core.stores.tenants import get_tenant
from daimon.core.support import (
    SupportError,
    SupportScope,
    format_credits,
    load_menu,
    parse_credits,
    parse_menu_key,
    parse_person_id,
    parse_scope,
)
from daimon.core.support_admin import (
    LimitChange,
    LimitUsage,
    expected_or_none,
    get_limit_usage,
    list_limit_usage,
    merge_limit_change,
    options_text,
    settle_outcome_text,
    window_text,
)
from daimon.core.support_changes import (
    ChannelPick,
    clear_limit,
    missing_menu,
    remove_menu,
    set_limit,
    set_menu,
    set_menu_use,
    set_support_channel,
    settle,
    stale_text,
)
from daimon.core.support_routing import own_channel_allowed, support_destination
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table
from sqlalchemy.ext.asyncio import AsyncSession

support_app = typer.Typer(help="Ask a person: credit limits, menus, the request channel, requests.")
limit_app = typer.Typer(help="Credit limits for everywhere, an agent or a channel.")
menu_app = typer.Typer(help="Menus of support options, and which place uses which.")
channel_app = typer.Typer(help="The workspace's own channel for support requests.")
requests_app = typer.Typer(help="Support requests: list, settle and decline.")
support_app.add_typer(limit_app, name="limit")
support_app.add_typer(menu_app, name="menu")
support_app.add_typer(channel_app, name="channel")
support_app.add_typer(requests_app, name="requests")

_SCOPE_HELP = "workspace (everywhere), agent or channel."
_ID_HELP = "Agent name or id, or channel or thread id; none for workspace."
_EXPECTED_LIMIT = typer.Option("--expected", help="Revision you last read, or `none`.")

ZERO = Decimal(0)

type Transport = httpx.AsyncBaseTransport | None


@support_app.callback()
def support_callback(
    ctx: typer.Context,
    tenant: Annotated[str | None, TENANT_OPTION] = None,
    guild: Annotated[str | None, GUILD_OPTION] = None,
) -> None:
    ctx.obj = TenantSelector(tenant_id=tenant, guild_id=guild)


@dataclass(frozen=True)
class _Target:
    """The tenant a command acts on and the operator's account in it."""

    tenant_id: uuid.UUID
    platform: str
    workspace_id: str
    account_id: uuid.UUID


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
    return _Target(tenant_id, tenant.platform, tenant.external_id, principal.account_id)


def _actor(target: _Target, *, command: str, reason: str) -> ChannelActor:
    """The operator as an admin; a write records `cli/support <command>` in its transaction."""

    async def audit(session: AsyncSession) -> None:
        await append_event(
            session,
            tenant_id=target.tenant_id,
            account_id=target.account_id,
            agent_id=None,
            platform=target.platform,
            platform_user_id=None,
            tool_name=f"cli/support {command}",
            operation=Action.SET_SUPPORT,
            outcome="allowed",
            reason=reason,
        )

    return ChannelActor(
        tenant_id=target.tenant_id,
        account_id=target.account_id,
        subject=Subject(is_admin=True),
        surface="cli",
        on_write=audit,
    )


async def _scope_id(
    rt: CliRuntime,
    target: _Target,
    scope: str,
    raw_id: str | None,
    *,
    transport: Transport,
    missing_ok: bool = False,
) -> tuple[SupportScope, str]:
    """An agent name becomes its id; a thread becomes its parent channel."""
    place_scope, scope_id = parse_scope(scope, raw_id)
    if place_scope == "agent":
        agent_id = await agent_id_in_tenant(
            rt.anthropic, tenant_id=target.tenant_id, ref=scope_id, records=rt.sessionmaker
        )
        if agent_id is None:
            raise StoreError(f"no agent {scope_id!r} in this workspace")
        scope_id = agent_id
    if place_scope == "channel":
        scope_id = await parent_channel(
            rt,
            platform=target.platform,
            workspace_id=target.workspace_id,
            channel_id=scope_id,
            transport=transport,
            missing_ok=missing_ok,
        )
    return place_scope, scope_id


async def _place_ref(
    rt: CliRuntime, target: _Target, ref: str, *, transport: Transport
) -> tuple[SupportScope, str]:
    """`workspace`, `agent:<name>` or `channel:<id>`."""
    scope, colon, raw_id = ref.partition(":")
    if not colon and scope.strip() != "workspace":
        raise SupportError(
            f"--place {ref} names no place. Use workspace, agent:<name> or channel:{ref.strip()}."
        )
    return await _scope_id(rt, target, scope, raw_id or None, transport=transport, missing_ok=True)


def _label(scope: SupportScope, scope_id: str, person: str | None = None) -> str:
    place = f"{scope} {scope_id}" if scope_id else "everywhere"
    return place if person is None else f"{person} at {place}"


def _expected_none_or[T](raw: str | None, parse: Callable[[str], T]) -> Expected[T | None] | None:
    try:
        return expected_or_none(raw, parse)
    except (ValueError, SupportError) as exc:
        raise typer.BadParameter(f"--expected: {exc}") from exc


def _stale(exc: StaleChannelValue) -> StoreError:
    return StoreError(stale_text(exc))


def _print_json(console: Console, payload: object) -> None:
    console.print(json.dumps(payload), soft_wrap=True, markup=False)


def _run(impl: Callable[[CliRuntime, Console], Awaitable[None]]) -> None:
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(load_settings()) as rt:
            await impl(rt, console)

    run_cli(run(), console=console)


def _revision(raw: str) -> int | None:
    """A revision; 0 means none, as in the MCP tool and the web page."""
    return int(raw) or None


def _table(console: Console, columns: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
    table = Table(show_header=True, header_style="bold")
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*(str(cell) for cell in row))
    console.print(table)


# ---------- menus ----------


async def menu_set(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    key: str,
    text: str,
    expected: str | None = None,
) -> None:
    target = await _target(rt, selector)
    spec = load_menu(text)
    try:
        row = await set_menu(
            rt.sessionmaker,
            _actor(target, command="menu set", reason=f"menu:{key}"),
            platform=target.platform,
            key=key,
            spec=spec,
            expected=_expected_none_or(expected, _revision),
        )
    except StaleChannelValue as exc:
        raise _stale(exc) from exc
    console.print(
        f"menu {row.key}: {row.name}, {len(spec.options)} options, revision {row.revision}"
    )


async def menu_show(
    *, rt: CliRuntime, console: Console, selector: TenantSelector | None, key: str, as_json: bool
) -> None:
    target = await _target(rt, selector)
    async with rt.sessionmaker() as session:
        row = await store.get_menu(session, tenant_id=target.tenant_id, key=key)
        if row is None:
            raise await missing_menu(session, tenant_id=target.tenant_id, key=key)
    body = row.spec.model_dump(mode="json", exclude_none=True)
    if as_json:
        _print_json(console, {"key": row.key, "revision": row.revision, "body": body})
        return
    console.print(f"# {row.key}, revision {row.revision}", markup=False)
    console.print(yaml.safe_dump(body, sort_keys=False, allow_unicode=True), markup=False)


class MenuListing(BaseModel):
    key: str
    name: str
    revision: int
    options: str
    used_by: list[str]


async def menu_list(
    *, rt: CliRuntime, console: Console, selector: TenantSelector | None, as_json: bool
) -> None:
    target = await _target(rt, selector)
    async with rt.sessionmaker() as session:
        rows = await store.list_menus(session, tenant_id=target.tenant_id)
        uses = await store.list_menu_uses(session, tenant_id=target.tenant_id)
    listing = [
        MenuListing(
            key=r.key,
            name=r.name,
            revision=r.revision,
            options=options_text(r.spec),
            used_by=[_label(u.scope, u.scope_id) for u in uses if u.menu_id == r.id],
        )
        for r in rows
    ]
    if as_json:
        _print_json(console, [item.model_dump(mode="json") for item in listing])
        return
    _table(
        console,
        ("key", "name", "revision", "options", "used by"),
        [(i.key, i.name, i.revision, i.options, ", ".join(i.used_by) or "-") for i in listing],
    )


async def menu_rm(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    key: str,
    expected: str | None = None,
) -> None:
    target = await _target(rt, selector)
    try:
        await remove_menu(
            rt.sessionmaker,
            _actor(target, command="menu rm", reason=f"menu:{key}"),
            platform=target.platform,
            key=key,
            expected=_expected_none_or(expected, _revision),
        )
    except StaleChannelValue as exc:
        raise _stale(exc) from exc
    console.print(f"menu {key}: removed", markup=False)


async def menu_use(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    key: str,
    scope: str,
    scope_id: str | None,
    expected: str | None = None,
    discord_transport: Transport = None,
) -> None:
    """`default` drops the place's own pick: the menu above it, or the built-in one, applies."""
    target = await _target(rt, selector)
    place_scope, limit_id = await _scope_id(
        rt, target, scope, scope_id, transport=discord_transport, missing_ok=key == "default"
    )
    try:
        await set_menu_use(
            rt.sessionmaker,
            _actor(target, command="menu use", reason=f"menu:{place_scope}:{limit_id}"),
            platform=target.platform,
            scope=place_scope,
            scope_id=limit_id,
            key=None if key == "default" else key,
            expected=_expected_none_or(expected, parse_menu_key),
        )
    except StaleChannelValue as exc:
        raise _stale(exc) from exc
    label = _label(place_scope, limit_id)
    what = "the default menu" if key == "default" else f"menu {key}"
    console.print(f"{label}: uses {what}", markup=False)


# ---------- limits ----------


def _limit_payload(usage: LimitUsage) -> dict[str, Any]:
    row = usage.limit
    return {
        "scope": row.scope,
        "scope_id": row.scope_id,
        "shared": None if row.shared is None else format_credits(row.shared),
        "each_person": None if row.each_person is None else format_credits(row.each_person),
        "window": row.window,
        "starts_at": row.starts_at.isoformat() if row.starts_at else None,
        "ends_at": row.ends_at.isoformat() if row.ends_at else None,
        "revision": row.revision,
        "menu": usage.menu,
        "shared_used": format_credits(usage.shared_used),
        "state": usage.state,
        "summary": usage.summary,
        "people": {p.platform_user_id: format_credits(p.each_person or ZERO) for p in usage.people},
    }


def _person_payload(row: SupportLimitRow) -> dict[str, Any]:
    return {
        "scope": row.scope,
        "scope_id": row.scope_id,
        "person": row.platform_user_id,
        "each_person": None if row.each_person is None else format_credits(row.each_person),
        "window": row.window,
        "starts_at": row.starts_at.isoformat() if row.starts_at else None,
        "ends_at": row.ends_at.isoformat() if row.ends_at else None,
        "revision": row.revision,
    }


def _limit_table(console: Console, usages: list[LimitUsage]) -> None:
    _table(
        console,
        ("place", "menu", "limit", "own limits"),
        [
            (
                _label(u.limit.scope, u.limit.scope_id),
                u.menu or "default",
                u.summary,
                ", ".join(
                    f"{p.platform_user_id}={format_credits(p.each_person or ZERO)}"
                    for p in u.people
                )
                or "-",
            )
            for u in usages
        ],
    )


async def limit_set(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    scope: str,
    scope_id: str | None,
    shared: str | None = None,
    each_person: str | None = None,
    window: str | None = None,
    starts_at: str | None = None,
    ends_at: str | None = None,
    person: str | None = None,
    expected: str | None = None,
    discord_transport: Transport = None,
) -> None:
    """Options left out keep the limit's values; `none` clears a credit amount or a date.

    With `person`, the limit is that person's own at the place (`workspace`: everywhere).
    """
    target = await _target(rt, selector)
    place_scope, limit_id = await _scope_id(
        rt, target, scope, scope_id, transport=discord_transport
    )
    who = None if person is None else parse_person_id(target.platform, person)
    change = LimitChange(shared, each_person, window, starts_at, ends_at)
    async with rt.sessionmaker() as session:
        spec, before = await merge_limit_change(
            session,
            tenant_id=target.tenant_id,
            scope=place_scope,
            scope_id=limit_id,
            change=change,
            platform_user_id=who,
        )
    try:
        row = await set_limit(
            rt.sessionmaker,
            _actor(target, command="limit set", reason=f"limit:{place_scope}:{limit_id}"),
            platform=target.platform,
            spec=spec,
            expected=_expected_none_or(expected, _revision) or Expected(before),
        )
    except StaleChannelValue as exc:
        raise _stale(exc) from exc
    if who is not None:
        console.print(
            f"{_label(place_scope, limit_id, who)}: {format_credits(row.each_person or ZERO)} "
            f"credits, {window_text(row, now=datetime.now(UTC))}",
            markup=False,
        )
        return
    async with rt.sessionmaker() as session:
        usage = await get_limit_usage(
            session,
            tenant_id=target.tenant_id,
            scope=place_scope,
            scope_id=limit_id,
            now=datetime.now(UTC),
        )
    assert usage is not None, "the limit just set"
    _limit_table(console, [usage])


async def limit_clear(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    scope: str,
    scope_id: str | None,
    person: str | None = None,
    expected: str | None = None,
    discord_transport: Transport = None,
) -> None:
    target = await _target(rt, selector)
    place_scope, limit_id = await _scope_id(
        rt, target, scope, scope_id, transport=discord_transport, missing_ok=True
    )
    who = None if person is None else parse_person_id(target.platform, person)
    try:
        cleared = await clear_limit(
            rt.sessionmaker,
            _actor(target, command="limit clear", reason=f"limit:{place_scope}:{limit_id}"),
            platform=target.platform,
            scope=place_scope,
            scope_id=limit_id,
            platform_user_id=who,
            expected=_expected_none_or(expected, _revision),
        )
    except StaleChannelValue as exc:
        raise _stale(exc) from exc
    label = _label(place_scope, limit_id, who)
    console.print(f"{label}: {'limit cleared' if cleared else 'had no limit'}", markup=False)


async def limit_list(
    *, rt: CliRuntime, console: Console, selector: TenantSelector | None, as_json: bool
) -> None:
    target = await _target(rt, selector)
    async with rt.sessionmaker() as session:
        usages = await list_limit_usage(session, tenant_id=target.tenant_id, now=datetime.now(UTC))
        people = await store.list_limits(session, tenant_id=target.tenant_id, people=True)
    if as_json:
        _print_json(
            console,
            {
                "limits": [_limit_payload(u) for u in usages],
                "people": [_person_payload(p) for p in people],
            },
        )
        return
    if not usages and not people:
        console.print("No limits. Requests use the default credits per person.")
        return
    if usages:
        _limit_table(console, usages)
    if people:
        _table(
            console,
            ("place", "person", "credits", "when"),
            [
                (
                    _label(p.scope, p.scope_id),
                    p.platform_user_id,
                    format_credits(p.each_person or ZERO),
                    window_text(p, now=datetime.now(UTC)),
                )
                for p in people
            ],
        )


# ---------- channel ----------


async def channel_set(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    channel_id: str,
    team: str | None = None,
    service_url: str | None = None,
    expected: str | None = None,
    discord_transport: Transport = None,
) -> None:
    """Send this workspace's support requests to `channel_id`; Teams also takes team and URL."""
    target = await _target(rt, selector)
    channel = await parent_channel(
        rt,
        platform=target.platform,
        workspace_id=target.workspace_id,
        channel_id=channel_id,
        transport=discord_transport,
    )
    if target.platform == "teams" and not (team and service_url):
        raise typer.BadParameter("Teams needs --team and --service-url")
    pick = ChannelPick(
        platform=target.platform,
        channel_id=channel,
        discord_guild_id=target.workspace_id if target.platform == "discord" else None,
        slack_team_id=target.workspace_id if target.platform == "slack" else None,
        teams_team_id=team if target.platform == "teams" else None,
        teams_service_url=service_url if target.platform == "teams" else None,
    )
    await _write_channel(rt, console, target, pick, expected, "channel set")
    console.print(f"support requests go to {channel}", markup=False)


async def channel_reset(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    expected: str | None = None,
) -> None:
    target = await _target(rt, selector)
    await _write_channel(rt, console, target, None, expected, "channel reset")
    console.print("support requests go to the default channel", markup=False)


async def _write_channel(
    rt: CliRuntime,
    console: Console,
    target: _Target,
    pick: ChannelPick | None,
    expected: str | None,
    command: str,
) -> None:
    try:
        await set_support_channel(
            rt.sessionmaker,
            _actor(target, command=command, reason="channel"),
            pick=pick,
            own_allowed=own_channel_allowed(rt.settings),
            expected=_expected_none_or(expected, _revision),
        )
    except StaleChannelValue as exc:
        raise _stale(exc) from exc


async def channel_show(
    *, rt: CliRuntime, console: Console, selector: TenantSelector | None, as_json: bool
) -> None:
    target = await _target(rt, selector)
    async with rt.sessionmaker() as session:
        own = await store.get_support_channel(session, tenant_id=target.tenant_id)
        destination = await support_destination(
            session, tenant_id=target.tenant_id, platform=target.platform, settings=rt.settings
        )
    allowed = own_channel_allowed(rt.settings)
    if as_json:
        _print_json(
            console,
            {
                "own_allowed": allowed,
                "destination": None
                if destination is None
                else {"channel_id": destination.channel_id, "own": destination.own},
                "own": None
                if own is None
                else {
                    "platform": own.platform,
                    "channel_id": own.channel_id,
                    "revision": own.revision,
                },
            },
        )
        return
    if destination is None:
        console.print("Requests go nowhere: no support channel is set.", markup=False)
    else:
        kind = "its own" if destination.own else "the default"
        console.print(f"Requests go to {destination.channel_id} ({kind} channel).", markup=False)
    if own is not None:
        console.print(f"Own channel: {own.channel_id}, revision {own.revision}.", markup=False)
    if not allowed:
        console.print("This deployment keeps its own request channel.", markup=False)


# ---------- requests ----------


class RequestListing(BaseModel):
    id: uuid.UUID
    created_at: datetime
    platform_user_id: str
    option: str
    status: str
    counted: str
    charged_to: str
    delivered: bool
    """False when no support channel received it: nobody on the team has seen it."""


def _listing(row: SupportEscalationRow) -> RequestListing:
    return RequestListing(
        id=row.id,
        created_at=row.created_at,
        platform_user_id=row.platform_user_id,
        option=row.option.label if row.option else "-",
        status=row.status,
        counted=format_credits(row.counted),
        charged_to=row.charged_to,
        delivered=row.delivered_at is not None,
    )


async def requests_list(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    pending: bool = False,
    undelivered: bool = False,
    place: str | None = None,
    limit: int = 50,
    as_json: bool = False,
    discord_transport: Transport = None,
) -> None:
    target = await _target(rt, selector)
    limit_id: uuid.UUID | None = None
    async with rt.sessionmaker() as session:
        if place is not None:
            scope, scope_id = await _place_ref(rt, target, place, transport=discord_transport)
            row = await store.get_limit(
                session, tenant_id=target.tenant_id, scope=scope, scope_id=scope_id
            )
            if row is None:
                raise StoreError(f"{_label(scope, scope_id)} has no limit")
            limit_id = row.id
        rows = await ledger.list_requests(
            session,
            tenant_id=target.tenant_id,
            status="pending" if pending and not undelivered else None,
            limit_id=limit_id,
            undelivered=undelivered,
            limit=limit,
        )
    listing = [_listing(r) for r in rows]
    if as_json:
        _print_json(console, [item.model_dump(mode="json") for item in listing])
        return
    _table(
        console,
        ("id", "created", "person", "option", "status", "credits", "delivered"),
        [
            (
                i.id,
                f"{i.created_at:%Y-%m-%d %H:%M}",
                i.platform_user_id,
                i.option,
                i.status,
                i.counted,
                "yes" if i.delivered else "no",
            )
            for i in listing
        ],
    )


async def requests_settle(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    request_id: str,
    charge: str | None,
    expected: str | None = None,
) -> None:
    """`charge` None declines. `expected` is the charge to correct on a settled request."""
    try:
        escalation_id = uuid.UUID(request_id)
    except ValueError as exc:
        raise typer.BadParameter(f"not a request id: {request_id!r}") from exc
    amount = None if charge is None else parse_credits(charge)
    correction: Decimal | None = None
    if expected is not None:
        correction = parse_credits(expected, name="--expected")
    target = await _target(rt, selector)
    command = "requests settle" if charge is not None else "requests decline"
    try:
        outcome = await settle(
            rt.sessionmaker,
            _actor(target, command=command, reason=f"request:{escalation_id}"),
            escalation_id=escalation_id,
            charge=amount,
            default_allowance=rt.settings.support.credits_per_user,
            expected=None if correction is None else Expected(correction),
        )
    except StaleChannelValue as exc:
        raise _stale(exc) from exc
    text = settle_outcome_text(outcome, correction=correction is not None)
    if outcome.status not in ("settled", "declined"):
        raise StoreError(text)
    console.print(text, markup=False)


# ---------- commands ----------


@menu_app.command("set")
def menu_set_command(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="Menu key: a-z, 0-9 and -.")],
    file: Annotated[Path, typer.Argument(help="YAML or JSON menu.", exists=True, dir_okay=False)],
    expected: Annotated[
        str | None, typer.Option("--expected", help="Revision you last read, or `none`.")
    ] = None,
) -> None:
    """Add or replace a menu of support options."""
    text = file.read_text(encoding="utf-8")
    _run(
        lambda rt, console: menu_set(
            rt=rt, console=console, selector=ctx.obj, key=key, text=text, expected=expected
        )
    )


@menu_app.command("show")
def menu_show_command(
    ctx: typer.Context, key: str, as_json: Annotated[bool, JSON_OPTION] = False
) -> None:
    """Print a menu as YAML that `menu set` takes back."""
    _run(
        lambda rt, console: menu_show(
            rt=rt, console=console, selector=ctx.obj, key=key, as_json=as_json
        )
    )


@menu_app.command("list")
def menu_list_command(ctx: typer.Context, as_json: Annotated[bool, JSON_OPTION] = False) -> None:
    """Every menu with its options, prices and the places using it."""
    _run(lambda rt, console: menu_list(rt=rt, console=console, selector=ctx.obj, as_json=as_json))


@menu_app.command("rm")
def menu_rm_command(
    ctx: typer.Context,
    key: str,
    expected: Annotated[
        str | None, typer.Option("--expected", help="Revision you last read.")
    ] = None,
) -> None:
    """Remove a menu no place uses."""
    _run(
        lambda rt, console: menu_rm(
            rt=rt, console=console, selector=ctx.obj, key=key, expected=expected
        )
    )


@menu_app.command("use")
def menu_use_command(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="Menu key, or `default` for the built-in menu.")],
    scope: Annotated[str, typer.Argument(help=_SCOPE_HELP)],
    scope_id: Annotated[str | None, typer.Argument(help=_ID_HELP)] = None,
    expected: Annotated[
        str | None, typer.Option("--expected", help="Menu key the place uses now, or `none`.")
    ] = None,
) -> None:
    """Pick the menu a place offers."""
    _run(
        lambda rt, console: menu_use(
            rt=rt,
            console=console,
            selector=ctx.obj,
            key=key,
            scope=scope,
            scope_id=scope_id,
            expected=expected,
        )
    )


@limit_app.command("set")
def limit_set_command(
    ctx: typer.Context,
    scope: Annotated[str, typer.Argument(help=_SCOPE_HELP)],
    scope_id: Annotated[str | None, typer.Argument(help=_ID_HELP)] = None,
    shared: Annotated[
        str | None, typer.Option("--shared", help="Credits everyone shares, or `none`.")
    ] = None,
    each_person: Annotated[
        str | None, typer.Option("--each-person", help="Credits each person may use, or `none`.")
    ] = None,
    window: Annotated[
        str | None, typer.Option("--window", help="total or monthly. New limits: total.")
    ] = None,
    starts_at: Annotated[
        str | None, typer.Option("--starts-at", help="ISO 8601, UTC without an offset, or `none`.")
    ] = None,
    ends_at: Annotated[
        str | None, typer.Option("--ends-at", help="ISO 8601, or `none`. Not counted from then.")
    ] = None,
    person: Annotated[
        str | None,
        typer.Option("--person", help="Set this person's own limit at the place instead."),
    ] = None,
    expected: Annotated[str | None, _EXPECTED_LIMIT] = None,
) -> None:
    """Set a limit; options left out keep the limit's values."""
    _run(
        lambda rt, console: limit_set(
            rt=rt,
            console=console,
            selector=ctx.obj,
            scope=scope,
            scope_id=scope_id,
            shared=shared,
            each_person=each_person,
            window=window,
            starts_at=starts_at,
            ends_at=ends_at,
            person=person,
            expected=expected,
        )
    )


@limit_app.command("clear")
def limit_clear_command(
    ctx: typer.Context,
    scope: Annotated[str, typer.Argument(help=_SCOPE_HELP)],
    scope_id: Annotated[str | None, typer.Argument(help=_ID_HELP)] = None,
    person: Annotated[
        str | None, typer.Option("--person", help="Clear this person's own limit instead.")
    ] = None,
    expected: Annotated[str | None, _EXPECTED_LIMIT] = None,
) -> None:
    """Remove a limit; a place's pending requests are declined, settled ones keep their charges."""
    _run(
        lambda rt, console: limit_clear(
            rt=rt,
            console=console,
            selector=ctx.obj,
            scope=scope,
            scope_id=scope_id,
            person=person,
            expected=expected,
        )
    )


@limit_app.command("list")
def limit_list_command(ctx: typer.Context, as_json: Annotated[bool, JSON_OPTION] = False) -> None:
    """Every limit with what it used, and each person's own limits."""
    _run(lambda rt, console: limit_list(rt=rt, console=console, selector=ctx.obj, as_json=as_json))


@channel_app.command("set")
def channel_set_command(
    ctx: typer.Context,
    channel_id: Annotated[str, typer.Argument(help="Channel to receive support requests.")],
    team: Annotated[str | None, typer.Option("--team", help="Teams: the team id.")] = None,
    service_url: Annotated[
        str | None, typer.Option("--service-url", help="Teams: the service URL.")
    ] = None,
    expected: Annotated[
        str | None, typer.Option("--expected", help="Revision you last read, or `none`.")
    ] = None,
) -> None:
    """Send this workspace's support requests to its own channel."""
    _run(
        lambda rt, console: channel_set(
            rt=rt,
            console=console,
            selector=ctx.obj,
            channel_id=channel_id,
            team=team,
            service_url=service_url,
            expected=expected,
        )
    )


@channel_app.command("show")
def channel_show_command(ctx: typer.Context, as_json: Annotated[bool, JSON_OPTION] = False) -> None:
    """Where support requests go now."""
    _run(
        lambda rt, console: channel_show(rt=rt, console=console, selector=ctx.obj, as_json=as_json)
    )


@channel_app.command("reset")
def channel_reset_command(
    ctx: typer.Context,
    expected: Annotated[
        str | None, typer.Option("--expected", help="Revision you last read, or `none`.")
    ] = None,
) -> None:
    """Go back to the default support channel."""
    _run(
        lambda rt, console: channel_reset(
            rt=rt, console=console, selector=ctx.obj, expected=expected
        )
    )


@requests_app.command("list")
def requests_list_command(
    ctx: typer.Context,
    pending: Annotated[bool, typer.Option("--pending", help="Only requests to settle.")] = False,
    undelivered: Annotated[
        bool,
        typer.Option("--undelivered", help="Only requests no channel received and nobody handled."),
    ] = False,
    place: Annotated[
        str | None, typer.Option("--place", help="workspace, agent:<name> or channel:<id>.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 50,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Requests, newest first."""
    _run(
        lambda rt, console: requests_list(
            rt=rt,
            console=console,
            selector=ctx.obj,
            pending=pending,
            undelivered=undelivered,
            place=place,
            limit=limit,
            as_json=as_json,
        )
    )


_EXPECTED_CHARGE = typer.Option(
    "--expected", help="To correct a settled request: the charge it has now."
)


@requests_app.command("settle")
def requests_settle_command(
    ctx: typer.Context,
    request_id: str,
    credits: Annotated[
        str, typer.Argument(help="The final charge in credits; a charged request keeps its own.")
    ],
    expected: Annotated[str | None, _EXPECTED_CHARGE] = None,
) -> None:
    """Charge a request. The client is not notified."""
    _run(
        lambda rt, console: requests_settle(
            rt=rt,
            console=console,
            selector=ctx.obj,
            request_id=request_id,
            charge=credits,
            expected=expected,
        )
    )


@requests_app.command("decline")
def requests_decline_command(
    ctx: typer.Context,
    request_id: str,
    expected: Annotated[str | None, _EXPECTED_CHARGE] = None,
) -> None:
    """Decline a request and refund its hold or charge. The client is not notified."""
    _run(
        lambda rt, console: requests_settle(
            rt=rt,
            console=console,
            selector=ctx.obj,
            request_id=request_id,
            charge=None,
            expected=expected,
        )
    )
