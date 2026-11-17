"""`/api/support-plans`: support plans, menus, own caps and undelivered requests, for admins.

Every write goes through `daimon.core.support_plan_changes` under `SET_SUPPORT_PLAN`,
the rule the CLI and MCP follow, sending the value last read: a value that moved since
is a 409. Each answer is the whole page, read fresh: plans and menus are never cached.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from daimon.adapters.web.access import is_admin
from daimon.adapters.web.admin_common import listed_agents
from daimon.adapters.web.audit import audit
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.channels import ChannelAdmin, channel_refusals, one_row
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.authz import Subject
from daimon.core.channel_changes import ChannelActor, Expected
from daimon.core.channel_directory import WEB_ROW_ID
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores import support_plans as store
from daimon.core.stores.platform_names import get_channel_names, get_user_names
from daimon.core.stores.support_rows import SupportAllowanceRow
from daimon.core.stores.tenants import get_tenant
from daimon.core.support_plan_admin import (
    PlanUsage,
    list_plan_usage,
    options_text,
    parse_plan_value,
)
from daimon.core.support_plan_changes import (
    PlanValue,
    clear_allowance,
    clear_plan,
    remove_catalog,
    set_allowance,
    set_catalog,
    set_plan,
)
from daimon.core.support_plans import (
    PlanScope,
    format_credits,
    parse_catalog,
    parse_catalog_key,
    parse_credits,
    parse_person_id,
    parse_plan_spec,
    parse_scope,
)
from daimon.core.windows import WindowState
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

_PLATFORMS = ("discord", "slack", "teams")
_UNAVAILABLE = "Support plans need a Discord, Slack or Teams workspace."
_PICK_CHANNEL = "Pick a channel, not a thread."
_PICK_AGENT = "Pick an agent from this workspace."
_PLAN_ACTION = "Changed support plan"
_MENU_ACTION = "Changed support menu"
_UNDELIVERED_SHOWN = 50

Window = Literal["monthly", "total", "fixed"]


class PersonCapJson(BaseModel):
    platform_user_id: str
    name: str | None
    credits: str


class PlanJson(BaseModel):
    scope: PlanScope
    scope_id: str
    name: str | None
    """The channel's or agent's name; None for the workspace or a name not known."""
    catalog: str | None
    pool: str | None
    per_person: str | None
    window: Window
    starts_at: str | None
    ends_at: str | None
    pool_used: str
    state: WindowState
    summary: str
    allowances: list[PersonCapJson]


class CatalogJson(BaseModel):
    key: str
    name: str
    revision: int
    summary: str
    catalog: dict[str, object]


class AgentChoiceJson(BaseModel):
    id: str
    name: str


class UndeliveredJson(BaseModel):
    """A request no support channel received: recorded and held, seen by nobody."""

    id: str
    created_at: str
    platform_user_id: str
    name: str | None
    option: str | None
    status: str
    counted: str


class SupportPlansJson(BaseModel):
    platform: str
    default_credits: str
    """What each person may use where no plan applies."""
    default_allowances: list[PersonCapJson]
    plans: list[PlanJson]
    catalogs: list[CatalogJson]
    agents: list[AgentChoiceJson]
    undelivered: list[UndeliveredJson]
    """Newest first, at most `_UNDELIVERED_SHOWN`."""


class PlanFields(BaseModel):
    catalog: str | None = None
    pool: str | None = None
    per_person: str | None = None
    window: Window = "total"
    starts_at: str | None = None
    ends_at: str | None = None


class PlanChange(BaseModel):
    scope: PlanScope
    scope_id: str = ""
    plan: PlanFields | None
    """None clears the plan."""
    expected: dict[str, object] | None
    """The plan as last read; None when there was none."""


class CatalogChange(BaseModel):
    catalog: dict[str, object] | None
    """None removes the menu."""
    expected_revision: int | None
    """The revision last read; None when there was none."""


class AllowanceChange(BaseModel):
    platform_user_id: str
    credits: str | None
    """None clears the person's own cap."""
    scope: PlanScope | None = None
    """None is the default, where no plan applies."""
    scope_id: str = ""
    expected: str | None


def _actor(caller: WebCaller, *, action: str, target: str, detail: str) -> ChannelActor:
    """The web admin acting, writing an audit row in the change's own transaction."""
    tenant_id = caller.person.tenant_id

    async def audited(db: AsyncSession) -> None:
        await audit(
            db,
            tenant_id=tenant_id,
            actor_account_id=caller.person.account_id,
            action=action,
            target=target,
            detail=detail,
            at=datetime.now(UTC),
        )

    return ChannelActor(
        tenant_id=tenant_id,
        account_id=caller.person.account_id,
        subject=Subject(is_admin=is_admin(caller.role)),
        surface="web",
        on_write=audited,
    )


def _caps(
    rows: tuple[SupportAllowanceRow, ...], names: dict[str, str | None]
) -> list[PersonCapJson]:
    return [
        PersonCapJson(
            platform_user_id=a.platform_user_id,
            name=names.get(a.platform_user_id),
            credits=format_credits(a.credits),
        )
        for a in rows
    ]


def _plan_json(usage: PlanUsage, *, name: str | None, people: dict[str, str | None]) -> PlanJson:
    value = usage.value.as_json()
    return PlanJson(
        scope=usage.plan.scope,
        scope_id=usage.plan.scope_id,
        name=name,
        catalog=usage.value.catalog,
        pool=_text(value["pool"]),
        per_person=_text(value["per_person"]),
        window=usage.value.window,
        starts_at=_text(value["starts_at"]),
        ends_at=_text(value["ends_at"]),
        pool_used=format_credits(usage.pool_used),
        state=usage.state,
        summary=usage.summary,
        allowances=_caps(usage.allowances, people),
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _expected_plan(raw: dict[str, object] | None) -> Expected[PlanValue | None]:
    return Expected(None if raw is None else parse_plan_value(raw))


def support_plans_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/support-plans")

    async def platform_of(caller: WebCaller) -> str:
        async with runtime.sessionmaker() as db:
            tenant = await get_tenant(db, caller.person.tenant_id)
        if tenant is None or tenant.platform not in _PLATFORMS:
            raise HTTPException(404, _UNAVAILABLE)
        return tenant.platform

    async def page(caller: WebCaller) -> SupportPlansJson:
        tenant_id = caller.person.tenant_id
        platform = await platform_of(caller)
        agents = {a.id: a.name for a in await listed_agents(runtime, tenant_id)}
        async with runtime.sessionmaker() as db:
            plans = await list_plan_usage(db, tenant_id=tenant_id, now=datetime.now(UTC))
            catalogs = await store.list_catalogs(db, tenant_id=tenant_id)
            defaults = tuple(await store.list_allowances(db, tenant_id=tenant_id, plan_id=None))
            lost = await ledger.list_requests(
                db, tenant_id=tenant_id, undelivered=True, limit=_UNDELIVERED_SHOWN
            )
            user_ids = {a.platform_user_id for p in plans for a in p.allowances}
            user_ids.update(a.platform_user_id for a in defaults)
            user_ids.update(r.platform_user_id for r in lost)
            known = await get_user_names(
                db, tenant_id=tenant_id, platform=platform, user_ids=user_ids
            )
            channels = await get_channel_names(
                db,
                tenant_id=tenant_id,
                platform=platform,
                channel_ids=[p.plan.scope_id for p in plans if p.plan.scope == "channel"],
            )
        people = {k: v.label for k, v in known.items()}
        names = {"channel": channels, "agent": agents}
        return SupportPlansJson(
            platform=platform,
            default_credits=format_credits(Decimal(runtime.settings.support.credits_per_user)),
            default_allowances=_caps(defaults, people),
            plans=[
                _plan_json(p, name=names.get(p.plan.scope, {}).get(p.plan.scope_id), people=people)
                for p in plans
            ],
            catalogs=[
                CatalogJson(
                    key=c.key,
                    name=c.spec.name,
                    revision=c.revision,
                    summary=options_text(c.spec),
                    catalog=c.spec.model_dump(mode="json", exclude_none=True),
                )
                for c in catalogs
            ],
            agents=[AgentChoiceJson(id=k, name=v) for k, v in agents.items()],
            undelivered=[
                UndeliveredJson(
                    id=str(r.id),
                    created_at=r.created_at.isoformat(),
                    platform_user_id=r.platform_user_id,
                    name=people.get(r.platform_user_id),
                    option=r.option.label if r.option else None,
                    status=r.status,
                    counted=format_credits(r.counted),
                )
                for r in lost
            ],
        )

    async def channel_of(caller: WebCaller, channel_id: str) -> str:
        """The channel a plan is set on: a thread's parent; 422 for web chat or no parent."""
        if channel_id == WEB_ROW_ID:
            raise HTTPException(422, "Web chat has no support plan.")
        row = await one_row(runtime, caller, channel_id)
        if row.kind == "channel":
            return row.channel_id
        if row.kind in ("thread", "forum_post"):
            parent = next((n.id for n in reversed(row.path) if n.kind == "channel"), None)
            if parent is not None:
                return parent
        raise HTTPException(422, _PICK_CHANNEL)

    async def check_agent(caller: WebCaller, agent_id: str) -> None:
        if agent_id not in {a.id for a in await listed_agents(runtime, caller.person.tenant_id)}:
            raise HTTPException(422, _PICK_AGENT)

    async def place_label(
        caller: WebCaller, platform: str, scope: PlanScope | None, scope_id: str
    ) -> str:
        """The audit target: the channel or agent by name, never a person."""
        if scope is None or scope == "workspace":
            return "Workspace"
        if scope == "agent":
            agents = await listed_agents(runtime, caller.person.tenant_id)
            return next((a.name for a in agents if a.id == scope_id), scope_id)
        async with runtime.sessionmaker() as db:
            names = await get_channel_names(
                db, tenant_id=caller.person.tenant_id, platform=platform, channel_ids=(scope_id,)
            )
        name = names.get(scope_id)
        return f"#{name}" if name else scope_id

    @router.get("")
    async def get_page(caller: ChannelAdmin) -> SupportPlansJson:  # pyright: ignore[reportUnusedFunction]
        return await page(caller)

    @router.put("/plan")
    async def put_plan(caller: ChannelAdmin, body: PlanChange) -> SupportPlansJson:  # pyright: ignore[reportUnusedFunction]
        platform = await platform_of(caller)
        with channel_refusals():
            scope, scope_id = parse_scope(body.scope, body.scope_id.strip() or None)
            expected = _expected_plan(body.expected)
            if body.plan is not None and scope == "channel":
                scope_id = await channel_of(caller, scope_id)
            if body.plan is not None and scope == "agent":
                await check_agent(caller, scope_id)
            target = await place_label(caller, platform, scope, scope_id)
            actor = _actor(caller, action=_PLAN_ACTION, target=target, detail="Plan")
            if body.plan is None:
                await clear_plan(
                    runtime.sessionmaker,
                    actor,
                    platform=platform,
                    scope=scope,
                    scope_id=scope_id,
                    expected=expected,
                )
            else:
                fields = body.plan
                spec = parse_plan_spec(
                    scope=scope,
                    scope_id=scope_id or None,
                    catalog=fields.catalog,
                    pool=fields.pool,
                    per_person=fields.per_person,
                    window=fields.window,
                    starts_at=fields.starts_at,
                    ends_at=fields.ends_at,
                )
                await set_plan(
                    runtime.sessionmaker, actor, platform=platform, spec=spec, expected=expected
                )
        return await page(caller)

    @router.put("/catalogs/{key}")
    async def put_catalog(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, key: str, body: CatalogChange
    ) -> SupportPlansJson:
        platform = await platform_of(caller)
        expected = Expected(body.expected_revision or None)
        with channel_refusals():
            key = parse_catalog_key(key)
            actor = _actor(caller, action=_MENU_ACTION, target=key, detail="Menu")
            if body.catalog is None:
                await remove_catalog(
                    runtime.sessionmaker, actor, platform=platform, key=key, expected=expected
                )
            else:
                await set_catalog(
                    runtime.sessionmaker,
                    actor,
                    platform=platform,
                    key=key,
                    spec=parse_catalog(body.catalog),
                    expected=expected,
                )
        return await page(caller)

    @router.put("/allowances")
    async def put_allowance(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, body: AllowanceChange
    ) -> SupportPlansJson:
        platform = await platform_of(caller)
        with channel_refusals():
            user_id = parse_person_id(platform, body.platform_user_id)
            scope, scope_id = (
                (None, "")
                if body.scope is None
                else parse_scope(body.scope, body.scope_id.strip() or None)
            )
            expected = Expected(
                None if body.expected is None else parse_credits(body.expected, name="expected")
            )
            if body.credits is not None and scope == "channel":
                scope_id = await channel_of(caller, scope_id)
            if body.credits is not None and scope == "agent":
                await check_agent(caller, scope_id)
            target = await place_label(caller, platform, scope, scope_id)
            actor = _actor(caller, action=_PLAN_ACTION, target=target, detail="Own cap")
            if body.credits is None:
                await clear_allowance(
                    runtime.sessionmaker,
                    actor,
                    platform=platform,
                    platform_user_id=user_id,
                    scope=scope,
                    scope_id=scope_id,
                    expected=expected,
                )
            else:
                await set_allowance(
                    runtime.sessionmaker,
                    actor,
                    platform=platform,
                    platform_user_id=user_id,
                    scope=scope,
                    credits=parse_credits(body.credits),
                    scope_id=scope_id,
                    expected=expected,
                )
        return await page(caller)

    return router
