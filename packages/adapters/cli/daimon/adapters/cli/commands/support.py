"""`daimon support catalog | plan | allowance | requests`: support plans for Ask a person.

The operator acts as an admin over the audited change service (`SET_SUPPORT_PLAN`),
the same rule the MCP tools and the web follow. Each write appends a security event
in the change's own transaction. Plans and menus are read per command, never cached.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, cast

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
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.errors import StaleChannelValue, StoreError
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores import support_plans as store
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.stores.security_audit import append_event
from daimon.core.stores.support_rows import SupportEscalationRow
from daimon.core.stores.tenants import get_tenant
from daimon.core.support_plan_admin import (
    PlanUsage,
    get_plan_usage,
    list_plan_usage,
    options_text,
    parse_plan_value,
    settle_outcome_text,
)
from daimon.core.support_plan_changes import (
    PlanValue,
    clear_allowance,
    clear_plan,
    remove_catalog,
    set_allowance,
    set_catalog,
    set_plan,
    settle,
)
from daimon.core.support_plans import (
    PlanScope,
    SupportPlanError,
    format_credits,
    load_catalog,
    parse_credits,
    parse_plan_spec,
    parse_scope,
)
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table
from sqlalchemy.ext.asyncio import AsyncSession

support_app = typer.Typer(help="Support plans for Ask a person: menus, plans, caps and requests.")
catalog_app = typer.Typer(help="Menus of support options.")
plan_app = typer.Typer(help="Credit plans for a workspace, an agent or a channel.")
allowance_app = typer.Typer(help="One person's own credit cap under a plan.")
requests_app = typer.Typer(help="Support requests: list, settle and decline.")
support_app.add_typer(catalog_app, name="catalog")
support_app.add_typer(plan_app, name="plan")
support_app.add_typer(allowance_app, name="allowance")
support_app.add_typer(requests_app, name="requests")

_SCOPE_HELP = "workspace, agent or channel."
_ID_HELP = "Agent name or id, or channel or thread id; none for workspace."
_PLAN = typer.Option(
    "--plan", help="The plan: workspace, agent:<name> or channel:<id>. Default: the default plan."
)
_EXPECTED_PLAN = typer.Option(
    "--expected", help="The plan you last read: `none`, or the JSON `plan show --json` printed."
)

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
            operation=Action.SET_SUPPORT_PLAN,
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
) -> tuple[PlanScope, str]:
    """An agent name becomes its id; a thread becomes its parent channel."""
    plan_scope, scope_id = parse_scope(scope, raw_id)
    if plan_scope == "agent" and not scope_id.startswith("agent_"):
        agent = await find_agent_by_daimon_tag(
            rt.anthropic, tenant_id=target.tenant_id, name=scope_id, records=rt.sessionmaker
        )
        if agent is None:
            raise StoreError(f"no agent named {scope_id!r} in this workspace")
        scope_id = agent.id
    if plan_scope == "channel":
        scope_id = await parent_channel(
            rt,
            platform=target.platform,
            workspace_id=target.workspace_id,
            channel_id=scope_id,
            transport=transport,
            missing_ok=missing_ok,
        )
    return plan_scope, scope_id


async def _plan_ref(
    rt: CliRuntime, target: _Target, ref: str | None, *, transport: Transport
) -> tuple[PlanScope | None, str]:
    """`--plan workspace|agent:<name>|channel:<id>`; None is the default plan."""
    if ref is None:
        return None, ""
    scope, _, raw_id = ref.partition(":")
    return await _scope_id(rt, target, scope, raw_id or None, transport=transport, missing_ok=True)


def _label(scope: PlanScope | None, scope_id: str) -> str:
    if scope is None:
        return "default plan"
    return f"{scope} {scope_id}" if scope_id else scope


def _expected_none_or[T](raw: str | None, parse: Callable[[str], T]) -> Expected[T | None] | None:
    if raw is None:
        return None
    if raw.strip().lower() == "none":
        return Expected(None)
    try:
        return Expected(parse(raw))
    except (ValueError, SupportPlanError) as exc:
        raise typer.BadParameter(f"--expected: {exc}") from exc


def _plan_json(raw: str) -> PlanValue:
    data: object = json.loads(raw)
    if not isinstance(data, dict):
        raise SupportPlanError("pass the JSON object `plan show --json` printed")
    return parse_plan_value(cast("dict[str, object]", data))


def _stale(exc: StaleChannelValue) -> StoreError:
    return StoreError(f"{exc} Now: {json.dumps(exc.current)}")


def _print_json(console: Console, payload: object) -> None:
    console.print(json.dumps(payload), soft_wrap=True, markup=False)


def _run(impl: Callable[[CliRuntime, Console], Awaitable[None]]) -> None:
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(load_settings()) as rt:
            await impl(rt, console)

    run_cli(run(), console=console)


def _revision(raw: str) -> int | None:
    """A menu revision; 0 means none, as in the MCP tool and the web page."""
    return int(raw) or None


# ---------- catalog ----------


async def catalog_set(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    key: str,
    text: str,
    expected: str | None = None,
) -> None:
    target = await _target(rt, selector)
    spec = load_catalog(text)
    try:
        row = await set_catalog(
            rt.sessionmaker,
            _actor(target, command="catalog set", reason=f"catalog:{key}"),
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


async def catalog_show(
    *, rt: CliRuntime, console: Console, selector: TenantSelector | None, key: str, as_json: bool
) -> None:
    target = await _target(rt, selector)
    async with rt.sessionmaker() as session:
        row = await store.get_catalog(session, tenant_id=target.tenant_id, key=key)
    if row is None:
        raise StoreError(f"no menu {key!r}")
    spec = row.spec.model_dump(mode="json", exclude_none=True)
    if as_json:
        _print_json(console, {"key": row.key, "revision": row.revision, "catalog": spec})
        return
    console.print(f"# {row.key}, revision {row.revision}", markup=False)
    console.print(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True), markup=False)


class CatalogListing(BaseModel):
    key: str
    name: str
    revision: int
    options: str


async def catalog_list(
    *, rt: CliRuntime, console: Console, selector: TenantSelector | None, as_json: bool
) -> None:
    target = await _target(rt, selector)
    async with rt.sessionmaker() as session:
        rows = await store.list_catalogs(session, tenant_id=target.tenant_id)
    listing = [
        CatalogListing(
            key=r.key,
            name=r.name,
            revision=r.revision,
            options=options_text(r.spec),
        )
        for r in rows
    ]
    if as_json:
        _print_json(console, [item.model_dump(mode="json") for item in listing])
        return
    _table(
        console,
        ("key", "name", "revision", "options"),
        [tuple(i.model_dump().values()) for i in listing],
    )


async def catalog_rm(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    key: str,
    expected: str | None = None,
) -> None:
    target = await _target(rt, selector)
    try:
        removed = await remove_catalog(
            rt.sessionmaker,
            _actor(target, command="catalog rm", reason=f"catalog:{key}"),
            platform=target.platform,
            key=key,
            expected=_expected_none_or(expected, _revision),
        )
    except StaleChannelValue as exc:
        raise _stale(exc) from exc
    console.print(f"menu {key}: {'removed' if removed else 'there was none'}")


# ---------- plan ----------


def _plan_payload(usage: PlanUsage) -> dict[str, Any]:
    return {
        **usage.value.as_json(),
        "pool_used": format_credits(usage.pool_used),
        "state": usage.state,
        "summary": usage.summary,
        "allowances": {a.platform_user_id: format_credits(a.credits) for a in usage.allowances},
    }


def _table(console: Console, columns: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
    table = Table(show_header=True, header_style="bold")
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*(str(cell) for cell in row))
    console.print(table)


async def plan_set(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    scope: str,
    scope_id: str | None,
    pool: str | None = None,
    per_person: str | None = None,
    catalog: str | None = None,
    window: str = "total",
    starts_at: str | None = None,
    ends_at: str | None = None,
    expected: str | None = None,
    discord_transport: Transport = None,
) -> None:
    target = await _target(rt, selector)
    plan_scope, plan_id = await _scope_id(rt, target, scope, scope_id, transport=discord_transport)
    spec = parse_plan_spec(
        scope=plan_scope,
        scope_id=plan_id or None,
        catalog=catalog,
        pool=pool,
        per_person=per_person,
        window=window,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    label = _label(plan_scope, plan_id)
    try:
        await set_plan(
            rt.sessionmaker,
            _actor(target, command="plan set", reason=f"plan:{plan_scope}:{plan_id}"),
            platform=target.platform,
            spec=spec,
            expected=_expected_none_or(expected, _plan_json),
        )
    except StaleChannelValue as exc:
        raise _stale(exc) from exc
    async with rt.sessionmaker() as session:
        usage = await get_plan_usage(
            session,
            tenant_id=target.tenant_id,
            scope=plan_scope,
            scope_id=plan_id,
            now=datetime.now(UTC),
        )
    console.print(f"{label}: {usage.summary if usage else 'set'}", markup=False)


async def plan_clear(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    scope: str,
    scope_id: str | None,
    expected: str | None = None,
    discord_transport: Transport = None,
) -> None:
    target = await _target(rt, selector)
    plan_scope, plan_id = await _scope_id(
        rt, target, scope, scope_id, transport=discord_transport, missing_ok=True
    )
    try:
        cleared = await clear_plan(
            rt.sessionmaker,
            _actor(target, command="plan clear", reason=f"plan:{plan_scope}:{plan_id}"),
            platform=target.platform,
            scope=plan_scope,
            scope_id=plan_id,
            expected=_expected_none_or(expected, _plan_json),
        )
    except StaleChannelValue as exc:
        raise _stale(exc) from exc
    label = _label(plan_scope, plan_id)
    console.print(f"{label}: {'plan cleared' if cleared else 'had no plan'}", markup=False)


async def plan_show(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    scope: str,
    scope_id: str | None,
    as_json: bool = False,
    discord_transport: Transport = None,
) -> None:
    target = await _target(rt, selector)
    plan_scope, plan_id = await _scope_id(
        rt, target, scope, scope_id, transport=discord_transport, missing_ok=True
    )
    async with rt.sessionmaker() as session:
        usage = await get_plan_usage(
            session,
            tenant_id=target.tenant_id,
            scope=plan_scope,
            scope_id=plan_id,
            now=datetime.now(UTC),
        )
    if usage is None:
        raise StoreError(f"{_label(plan_scope, plan_id)} has no plan")
    if as_json:
        _print_json(console, _plan_payload(usage))
        return
    _plan_table(console, [usage])


def _plan_table(console: Console, plans: list[PlanUsage]) -> None:
    _table(
        console,
        ("scope", "id", "menu", "summary", "own caps"),
        [
            (
                u.plan.scope,
                u.plan.scope_id or "-",
                u.value.catalog or "default",
                u.summary,
                ", ".join(f"{a.platform_user_id}={format_credits(a.credits)}" for a in u.allowances)
                or "-",
            )
            for u in plans
        ],
    )


async def plan_list(
    *, rt: CliRuntime, console: Console, selector: TenantSelector | None, as_json: bool
) -> None:
    target = await _target(rt, selector)
    async with rt.sessionmaker() as session:
        plans = await list_plan_usage(session, tenant_id=target.tenant_id, now=datetime.now(UTC))
    if as_json:
        _print_json(console, [_plan_payload(u) for u in plans])
        return
    if not plans:
        console.print("No support plans. Requests use the default allowance.")
        return
    _plan_table(console, plans)


# ---------- allowance ----------


async def allowance_set(
    *,
    rt: CliRuntime,
    console: Console,
    selector: TenantSelector | None,
    platform_user_id: str,
    credits: str | None,
    plan: str | None = None,
    expected: str | None = None,
    discord_transport: Transport = None,
) -> None:
    """`credits` None clears the person's own cap."""
    target = await _target(rt, selector)
    scope, scope_id = await _plan_ref(rt, target, plan, transport=discord_transport)
    command = "allowance set" if credits is not None else "allowance clear"
    actor = _actor(target, command=command, reason=f"allowance:{scope or 'default'}:{scope_id}")
    wanted = _expected_none_or(expected, lambda raw: parse_credits(raw, name="--expected"))
    label = _label(scope, scope_id)
    try:
        if credits is None:
            cleared = await clear_allowance(
                rt.sessionmaker,
                actor,
                platform=target.platform,
                platform_user_id=platform_user_id,
                scope=scope,
                scope_id=scope_id,
                expected=wanted,
            )
            done = "cap cleared" if cleared else "had no own cap"
        else:
            amount = parse_credits(credits)
            await set_allowance(
                rt.sessionmaker,
                actor,
                platform=target.platform,
                platform_user_id=platform_user_id,
                credits=amount,
                scope=scope,
                scope_id=scope_id,
                expected=wanted,
            )
            done = f"cap {format_credits(amount)}"
    except StaleChannelValue as exc:
        raise _stale(exc) from exc
    console.print(f"{platform_user_id} under the {label}: {done}", markup=False)


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
    plan: str | None = None,
    limit: int = 50,
    as_json: bool = False,
    discord_transport: Transport = None,
) -> None:
    target = await _target(rt, selector)
    plan_id: uuid.UUID | None = None
    async with rt.sessionmaker() as session:
        if plan is not None:
            scope, scope_id = await _plan_ref(rt, target, plan, transport=discord_transport)
            if scope is None:
                raise StoreError("--plan names a workspace, agent or channel plan")
            row = await store.get_plan(
                session, tenant_id=target.tenant_id, scope=scope, scope_id=scope_id
            )
            if row is None:
                raise StoreError(f"{_label(scope, scope_id)} has no plan")
            plan_id = row.id
        rows = await ledger.list_requests(
            session,
            tenant_id=target.tenant_id,
            status="pending" if pending else None,
            plan_id=plan_id,
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


@catalog_app.command("set")
def catalog_set_command(
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
        lambda rt, console: catalog_set(
            rt=rt, console=console, selector=ctx.obj, key=key, text=text, expected=expected
        )
    )


@catalog_app.command("show")
def catalog_show_command(
    ctx: typer.Context, key: str, as_json: Annotated[bool, JSON_OPTION] = False
) -> None:
    """Print a menu as YAML that `catalog set` takes back."""
    _run(
        lambda rt, console: catalog_show(
            rt=rt, console=console, selector=ctx.obj, key=key, as_json=as_json
        )
    )


@catalog_app.command("list")
def catalog_list_command(ctx: typer.Context, as_json: Annotated[bool, JSON_OPTION] = False) -> None:
    """Every menu with its options and prices."""
    _run(
        lambda rt, console: catalog_list(rt=rt, console=console, selector=ctx.obj, as_json=as_json)
    )


@catalog_app.command("rm")
def catalog_rm_command(
    ctx: typer.Context,
    key: str,
    expected: Annotated[
        str | None, typer.Option("--expected", help="Revision you last read.")
    ] = None,
) -> None:
    """Remove a menu no plan uses."""
    _run(
        lambda rt, console: catalog_rm(
            rt=rt, console=console, selector=ctx.obj, key=key, expected=expected
        )
    )


@plan_app.command("set")
def plan_set_command(
    ctx: typer.Context,
    scope: Annotated[str, typer.Argument(help=_SCOPE_HELP)],
    scope_id: Annotated[str | None, typer.Argument(help=_ID_HELP)] = None,
    pool: Annotated[str | None, typer.Option("--pool", help="Credits shared by everyone.")] = None,
    per_person: Annotated[
        str | None, typer.Option("--per-person", help="Credits each person may use.")
    ] = None,
    catalog: Annotated[
        str | None, typer.Option("--catalog", help="Menu key, or `default`.")
    ] = None,
    window: Annotated[str, typer.Option("--window", help="total, monthly or fixed.")] = "total",
    starts_at: Annotated[
        str | None, typer.Option("--starts-at", help="ISO 8601, UTC without an offset.")
    ] = None,
    ends_at: Annotated[
        str | None, typer.Option("--ends-at", help="ISO 8601; fixed windows only.")
    ] = None,
    expected: Annotated[str | None, _EXPECTED_PLAN] = None,
) -> None:
    """Set or replace a plan."""
    _run(
        lambda rt, console: plan_set(
            rt=rt,
            console=console,
            selector=ctx.obj,
            scope=scope,
            scope_id=scope_id,
            pool=pool,
            per_person=per_person,
            catalog=catalog,
            window=window,
            starts_at=starts_at,
            ends_at=ends_at,
            expected=expected,
        )
    )


@plan_app.command("clear")
def plan_clear_command(
    ctx: typer.Context,
    scope: Annotated[str, typer.Argument(help=_SCOPE_HELP)],
    scope_id: Annotated[str | None, typer.Argument(help=_ID_HELP)] = None,
    expected: Annotated[str | None, _EXPECTED_PLAN] = None,
) -> None:
    """Remove a plan; its requests keep their charges."""
    _run(
        lambda rt, console: plan_clear(
            rt=rt,
            console=console,
            selector=ctx.obj,
            scope=scope,
            scope_id=scope_id,
            expected=expected,
        )
    )


@plan_app.command("show")
def plan_show_command(
    ctx: typer.Context,
    scope: Annotated[str, typer.Argument(help=_SCOPE_HELP)],
    scope_id: Annotated[str | None, typer.Argument(help=_ID_HELP)] = None,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """One plan with what it used."""
    _run(
        lambda rt, console: plan_show(
            rt=rt,
            console=console,
            selector=ctx.obj,
            scope=scope,
            scope_id=scope_id,
            as_json=as_json,
        )
    )


@plan_app.command("list")
def plan_list_command(ctx: typer.Context, as_json: Annotated[bool, JSON_OPTION] = False) -> None:
    """Every plan with what it used."""
    _run(lambda rt, console: plan_list(rt=rt, console=console, selector=ctx.obj, as_json=as_json))


_EXPECTED_CREDITS = typer.Option("--expected", help="Credits you last read, or `none`.")


@allowance_app.command("set")
def allowance_set_command(
    ctx: typer.Context,
    platform_user_id: Annotated[str, typer.Argument(help="The person's platform user id.")],
    credits: Annotated[str, typer.Argument(help="Their own cap in credits.")],
    plan: Annotated[str | None, _PLAN] = None,
    expected: Annotated[str | None, _EXPECTED_CREDITS] = None,
) -> None:
    """Give one person their own cap under a plan."""
    _run(
        lambda rt, console: allowance_set(
            rt=rt,
            console=console,
            selector=ctx.obj,
            platform_user_id=platform_user_id,
            credits=credits,
            plan=plan,
            expected=expected,
        )
    )


@allowance_app.command("clear")
def allowance_clear_command(
    ctx: typer.Context,
    platform_user_id: Annotated[str, typer.Argument(help="The person's platform user id.")],
    plan: Annotated[str | None, _PLAN] = None,
    expected: Annotated[str | None, _EXPECTED_CREDITS] = None,
) -> None:
    """Put one person back on the plan's per-person limit."""
    _run(
        lambda rt, console: allowance_set(
            rt=rt,
            console=console,
            selector=ctx.obj,
            platform_user_id=platform_user_id,
            credits=None,
            plan=plan,
            expected=expected,
        )
    )


@requests_app.command("list")
def requests_list_command(
    ctx: typer.Context,
    pending: Annotated[bool, typer.Option("--pending", help="Only requests to settle.")] = False,
    undelivered: Annotated[
        bool, typer.Option("--undelivered", help="Only requests no support channel received.")
    ] = False,
    plan: Annotated[
        str | None, typer.Option("--plan", help="workspace, agent:<name> or channel:<id>.")
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
            plan=plan,
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
    credits: Annotated[str, typer.Argument(help="The final charge in credits.")],
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
    """Decline a request and refund its hold. The client is not notified."""
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
