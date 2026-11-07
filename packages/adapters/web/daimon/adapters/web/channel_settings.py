"""`/api/channels/{id}/settings` and its writes, and `/api/channel-defaults`.

A channel's further settings (following threads, progress plans, sandbox, skills,
tool rules, provider pin, its copy, its threads' own settings) and what its page
reads beyond the row (spend three ways, routines, link, Teams subscription). Every
write goes through `daimon.core.channel_settings` or, for the Settings group (approvals,
thread participation, progress plans, the cost footer), `daimon.core.scope_settings`
with the value the admin last read; members read only. Approvals need the person's
linked chat account to be a native admin of the platform, checked live on each write.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.access import is_admin
from daimon.adapters.web.admin_common import listed_agents
from daimon.adapters.web.caller import WebCaller, requires
from daimon.adapters.web.channel_agents import AgentRefJson, agent_refs
from daimon.adapters.web.channels import (
    ChangeResultJson,
    actor_of,
    channel_refusals,
    detail_of,
    one_row,
)
from daimon.adapters.web.routines import routine_title
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import routine_service
from daimon.core.admin_actions import DelegatedAction
from daimon.core.agent_pins import agent_pin_names
from daimon.core.channel_changes import Expected
from daimon.core.channel_directory import WEB_ROW_ID, ValueSource, load_channel_directory
from daimon.core.channel_reads import (
    RoutineRelation,
    channel_routines,
    channel_spend,
    platform_link,
)
from daimon.core.channel_reads import teams_subscription as read_subscription
from daimon.core.channel_settings import (
    add_skill,
    clear_provider_pin,
    clear_thread_override,
    list_thread_overrides,
    provider_pin,
    remove_skill,
    retire_channel_copy,
    set_environment,
    set_tool_rule,
    set_workspace_default_agent,
)
from daimon.core.channel_switches import SWITCH_PLATFORMS, own_mode
from daimon.core.credit_views import recent_start
from daimon.core.defaults.loader import load_platform_skills
from daimon.core.defaults.metadata import MA_METADATA_KEY_CHANNEL_COPY
from daimon.core.errors import SkillsListTruncatedError
from daimon.core.routine_service import RoutineCaller, RoutineRefused
from daimon.core.scope_cascade import CascadeMode, CascadeScope, resolve_modes
from daimon.core.scope_settings import (
    SettingKey,
    SettingLevel,
    SettingsActor,
    SettingsDefaults,
    SettingsPlace,
    SettingView,
    change_setting,
    read_settings,
)
from daimon.core.stores import agent_tool_rules as tool_rules
from daimon.core.stores.channel_changes import WORKSPACE_ID
from daimon.core.stores.channel_skills import list_channel_skills
from daimon.core.stores.domain import CHAT_PLATFORMS, RoutineRow
from daimon.core.stores.platform_names import get_channel_names, get_user_names
from daimon.core.stores.switch_modes import Switch, get_switch_modes
from daimon.core.stores.tenants import get_tenant
from daimon.core.thread_models import model_label
from fastapi import APIRouter, Depends, HTTPException
from mux.contracts.config import ConfigRevision
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

ChannelViewer = Annotated[WebCaller, Depends(requires("channels.view"))]
ChannelAdmin = Annotated[WebCaller, Depends(requires("channels.manage"))]
WorkspaceAdmin = Annotated[WebCaller, Depends(requires("workspace.manage"))]
Mode = Literal["on", "off", "disabled"]
ApprovalMode = Literal["on", "off"]
_SPEND_DAYS = 30
_NO_WEB = "Web chat has no such setting."
_NO_AGENT = "Pick an agent from this workspace."


class SwitchJson(BaseModel):
    available: bool
    own: Mode | None
    """The channel's (or workspace's) own mode; None while it inherits."""
    value: Mode
    source: Literal["deployment", "workspace", "channel", "thread"]


class ApprovalsJson(BaseModel):
    available: bool
    own: ApprovalMode | None
    """The channel's (or workspace's) own value; None while it inherits."""
    value: ApprovalMode
    """What is in effect: `on` asks before third-party writes, `off` allows them."""
    source: Literal["default", "server", "channel", "thread"]
    active: bool
    """False until the operator enables approval overrides: saved but ignored."""
    editable: bool
    """Whether the person has a linked chat account that may try (checked live on save)."""


class CostFooterJson(BaseModel):
    shown: bool
    editable: bool


class SkillJson(BaseModel):
    skill_id: str
    name: str


class ToolRuleJson(BaseModel):
    agent: str
    tool: str
    tool_label: str
    """The tool as people read it, without its server."""
    server_name: str
    rule: Literal["allow", "ask", "deny"] | None
    scope: Literal["chat", "chat_and_routines"]
    revision: int


class ToolRulesJson(BaseModel):
    editable: bool
    rules: list[ToolRuleJson]


class PinJson(BaseModel):
    backend: str
    model: str | None
    model_label: str | None
    local: int


class CopyJson(BaseModel):
    name: str
    agent_id: str


class ThreadJson(BaseModel):
    thread_id: str
    name: str | None
    """What the platform calls the thread; None when it was never seen."""
    kind: Literal["participation", "progress_plans", "thread_rule", "handoff"]
    value: str


class SubscriptionJson(BaseModel):
    status: Literal["active", "expired", "missing"]
    expires_at: str | None


class RoutineJson(BaseModel):
    id: str
    title: str
    """Its name, else the first line of its prompt."""
    agent: str
    cron: str
    enabled: bool
    relation: Literal["posts_here", "made_here", "both"]


class AmountJson(BaseModel):
    key: str | None
    """A day (YYYY-MM-DD), agent id or person id; None when unknown."""
    name: str | None
    usd: float


class SpendBreakdownJson(BaseModel):
    days: int
    by_day: list[AmountJson]
    by_agent: list[AmountJson]
    by_person: list[AmountJson]


class ChannelSettingsJson(BaseModel):
    participation: SwitchJson
    progress_plans: SwitchJson
    approvals: ApprovalsJson
    skills: list[SkillJson]
    tool_rules: ToolRulesJson
    provider_pin: PinJson | None
    channel_copy: CopyJson | None
    threads: list[ThreadJson]
    link: str | None
    teams_subscription: SubscriptionJson | None
    routines: list[RoutineJson]
    agents: dict[str, AgentRefJson]
    """The agents the tool rules and routines name, by key."""
    spend: SpendBreakdownJson | None
    """None for a member: spend figures are an admin's."""


class SourcedValueJson(BaseModel):
    value: str | None
    source: ValueSource | None


class DefaultsJson(BaseModel):
    platform: str
    agent: SourcedValueJson
    environment: SourcedValueJson
    participation: SwitchJson
    progress_plans: SwitchJson
    approvals: ApprovalsJson
    cost_footer: CostFooterJson


class SwitchChange(BaseModel):
    mode: Mode | None
    expected: Mode | None


class ApprovalsChange(BaseModel):
    mode: ApprovalMode | None
    """None inherits."""
    expected: ApprovalMode | None


class CostFooterChange(BaseModel):
    shown: bool
    expected: bool


class EnvironmentChange(BaseModel):
    environment: str | None
    expected: str | None
    confirm_open_network: bool = False


class SkillChange(BaseModel):
    action: Literal["add", "remove"]
    skill: str
    """A name or id to add; the skill id to remove."""
    expected: list[str]
    """The skill ids last read."""


class ToolRuleChange(BaseModel):
    agent: str
    tool: str
    rule: Literal["allow", "ask", "deny"] | None
    scope: Literal["chat", "chat_and_routines"] = "chat"
    expected_revision: int


class PinClear(BaseModel):
    expected_local: int


class CopyRetire(BaseModel):
    name: str
    agent_id: str


class ThreadClear(BaseModel):
    thread_id: str
    kind: Literal["participation", "progress_plans", "thread_rule", "handoff"]


class DefaultAgentChange(BaseModel):
    agent: str | None
    expected: str | None


def _humanise(key: str) -> str:
    words = key.replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:]


def split_tool(tool_key: str) -> tuple[str, str]:
    """A tool key `server/tool` as a server name and a tool label, both in words."""
    server, _, tool = tool_key.partition("/")
    return (_humanise(server), _humanise(tool)) if tool else ("", _humanise(server))


def _pin(pin: ConfigRevision) -> PinJson:
    return PinJson(
        backend=pin.backend,
        model=pin.model,
        model_label=model_label(pin.model) if pin.model else None,
        local=pin.local,
    )


def _mode(mode: CascadeMode | None) -> Mode | None:
    return None if mode is None else mode.value


def _approval(value: str | None) -> ApprovalMode:
    return "off" if value == "off" else "on"


def _approval_source(view: SettingView) -> Literal["default", "server", "channel", "thread"]:
    decided = view.decided_by
    if decided in ("server", "channel", "thread"):
        return decided
    return "default"


def channel_settings_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter()
    settings = runtime.settings

    def deployment(switch: Switch, platform: str) -> CascadeMode:
        if switch == "progress_plans":
            return settings.progress_plans.mode
        configured = settings.teams if platform == "teams" else settings.discord
        return CascadeMode.DISABLED if configured is None else settings.thread_participation.mode

    async def switch_json(
        db: AsyncSession,
        switch: Switch,
        tenant_id: uuid.UUID,
        platform: str,
        channel_id: str | None,
    ) -> SwitchJson:
        modes = await get_switch_modes(
            db,
            switch,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel_id,
            thread_id=None,
        )
        scope = CascadeScope.WORKSPACE if channel_id is None else CascadeScope.CHANNEL
        effective = resolve_modes(deployment(switch, platform), modes)
        return SwitchJson(
            available=platform in SWITCH_PLATFORMS[switch],
            own=_mode(own_mode(modes, scope)),
            value=effective.mode.value,
            source=effective.tier,
        )

    async def native_user_id(caller: WebCaller, platform: str) -> str | None:
        """The person's linked chat account on the workspace's platform, if any."""
        async with runtime.sessionmaker() as db:
            principal = await caller.principal(db)
        return principal.external_id if principal.platform == platform else None

    async def approvals_json(
        caller: WebCaller, tenant_id: uuid.UUID, platform: str, channel_id: str | None
    ) -> ApprovalsJson:
        place = SettingsPlace(platform, channel_id)
        (view,) = await read_settings(
            runtime.sessionmaker,
            tenant_id=tenant_id,
            place=place,
            defaults=SettingsDefaults.from_settings(settings, platform),
            keys=("approvals",),
        )
        own = view.own("channel" if channel_id is not None else "server")
        linked = await native_user_id(caller, platform)
        return ApprovalsJson(
            available=view.available,
            own=None if own in (None, "inherit") else _approval(own),
            value=_approval(view.effective),
            source=_approval_source(view),
            active=view.active,
            editable=view.available
            and "channels.manage" in caller.capabilities
            and linked is not None,
        )

    async def member_routines(
        caller: WebCaller, found: list[tuple[RoutineRow, RoutineRelation]]
    ) -> list[tuple[RoutineRow, RoutineRelation]]:
        """The routines a member may list in `routine_service.list_routines`, no others."""
        if not found:
            return []
        client = runtime.anthropic or (runtime.turns.deps.anthropic if runtime.turns else None)
        if client is None:
            return []
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            principal = await caller.principal(db)
        try:
            view = await routine_service.isolation_view_loader(
                runtime.sessionmaker, client, tenant_id
            )()
        except RoutineRefused:
            return []
        acting = RoutineCaller(
            tenant_id=tenant_id, subject=principal.subject, platform=principal.platform
        )
        shown = {
            r.id
            for r in routine_service.readable_routines(acting, view.viewer, [r for r, _ in found])
        }
        return [(r, rel) for r, rel in found if r.id in shown]

    async def agents(caller: WebCaller) -> list[BetaManagedAgentsAgent]:
        return await listed_agents(runtime, caller.person.tenant_id)

    async def agent_named(caller: WebCaller, name: str) -> BetaManagedAgentsAgent:
        for agent in await agents(caller):
            if name in {n for n in agent_pin_names(agent.name, agent.metadata) if n}:
                return agent
        raise HTTPException(422, _NO_AGENT)

    def tool_safety() -> bool:
        return settings.tool_safety.enabled and settings.tool_safety.agent_rules

    def client():
        if runtime.anthropic is None:
            raise HTTPException(503, "Agents are not available right now.")
        return runtime.anthropic

    async def platform_row(caller: WebCaller, channel_id: str) -> str:
        if channel_id == WEB_ROW_ID:
            raise HTTPException(422, _NO_WEB)
        return (await one_row(runtime, caller, channel_id)).platform

    async def changed(caller: WebCaller, channel_id: str) -> ChangeResultJson:
        row = await one_row(runtime, caller, channel_id)
        detail = await detail_of(runtime, caller, row, manage=True, figures=True)
        return ChangeResultJson(channel=detail, notes=[])

    @router.get("/api/channels/{channel_id}/settings")
    async def get_settings(caller: ChannelViewer, channel_id: str) -> ChannelSettingsJson:  # pyright: ignore[reportUnusedFunction]
        row = await one_row(runtime, caller, channel_id)
        tenant_id, platform = caller.person.tenant_id, row.platform
        listed = await agents(caller)
        by_id = {a.id: a.name for a in listed}
        async with runtime.sessionmaker() as db:
            tenant = await get_tenant(db, tenant_id)
            skills = await list_channel_skills(
                db, tenant_id=tenant_id, platform=platform, channel_id=channel_id
            )
            rules = await tool_rules.list_rules(db, tenant_id=tenant_id, channel_id=channel_id)
            pin = await provider_pin(
                db, tenant_id=tenant_id, platform=platform, channel_id=channel_id
            )
            threads = await list_thread_overrides(
                db, tenant_id=tenant_id, platform=platform, channel_id=channel_id
            )
            thread_names = await get_channel_names(
                db,
                tenant_id=tenant_id,
                platform=platform,
                channel_ids=[t.thread_id for t in threads],
            )
            name = row.name
            sub = await read_subscription(
                db,
                tenant_id=tenant_id,
                channel_id=channel_id,
                kind=name.kind if name else None,
                now=datetime.now(UTC),
            )
            routines = await channel_routines(db, tenant_id=tenant_id, channel_id=channel_id)
            spend = (
                await channel_spend(
                    db,
                    tenant_id=tenant_id,
                    channel_id=channel_id,
                    since=recent_start(datetime.now(UTC)),
                    markup=settings.billing.markup,
                )
                if is_admin(caller.role)
                else None
            )
            people = (
                await get_user_names(
                    db,
                    tenant_id=tenant_id,
                    platform=platform,
                    user_ids=[p for p in spend.by_person if p is not None],
                )
                if spend is not None
                else {}
            )
            participation = await switch_json(db, "participation", tenant_id, platform, channel_id)
            plans = await switch_json(db, "progress_plans", tenant_id, platform, channel_id)
        approvals = await approvals_json(caller, tenant_id, platform, channel_id)
        copy = next(
            (a for a in listed if a.metadata.get(MA_METADATA_KEY_CHANNEL_COPY) == channel_id),
            None,
        )
        person_name = {k: v.display_name or v.handle for k, v in people.items()}
        if not is_admin(caller.role):
            routines = await member_routines(caller, routines)
        refs = await agent_refs(
            runtime,
            caller,
            [
                *(r.agent_name for r in rules),
                *(r.agent_name for r, _ in routines),
                *(t.value for t in threads if t.kind == "handoff"),
            ],
        )
        return ChannelSettingsJson(
            participation=participation,
            progress_plans=plans,
            approvals=approvals,
            skills=[SkillJson(skill_id=s.skill_id, name=s.name) for s in skills],
            tool_rules=ToolRulesJson(
                editable=tool_safety() and is_admin(caller.role),
                rules=[
                    ToolRuleJson(
                        agent=r.agent_name,
                        tool=r.tool_key,
                        server_name=split_tool(r.tool_key)[0],
                        tool_label=split_tool(r.tool_key)[1],
                        rule=r.rule,
                        scope=r.scope,
                        revision=r.revision,
                    )
                    for r in rules
                ],
            ),
            provider_pin=_pin(pin) if pin is not None else None,
            channel_copy=CopyJson(name=copy.name, agent_id=copy.id) if copy else None,
            threads=[
                ThreadJson(
                    thread_id=t.thread_id,
                    name=thread_names.get(t.thread_id),
                    kind=t.kind,
                    value=t.value,
                )
                for t in threads
            ],
            link=platform_link(
                platform,
                workspace_id=tenant.external_id if tenant else "",
                channel_id=channel_id,
                name=name.name if name else None,
                group_id=sub[1].group_id if sub and sub[1] else None,
            ),
            teams_subscription=SubscriptionJson(
                status=sub[0], expires_at=sub[1].expires_at.isoformat() if sub[1] else None
            )
            if sub is not None
            else None,
            routines=[
                RoutineJson(
                    id=str(r.id),
                    title=routine_title(r.name, r.trigger_message),
                    agent=r.agent_name,
                    cron=r.cron_expr,
                    enabled=r.enabled,
                    relation=rel,
                )
                for r, rel in routines
            ],
            agents=refs,
            spend=None
            if spend is None
            else SpendBreakdownJson(
                days=_SPEND_DAYS,
                by_day=[
                    AmountJson(key=d.isoformat(), name=None, usd=float(v))
                    for d, v in spend.by_day.items()
                ],
                by_agent=[
                    AmountJson(key=k, name=by_id.get(k) if k else None, usd=float(v))
                    for k, v in spend.by_agent.items()
                ],
                by_person=[
                    AmountJson(key=k, name=person_name.get(k) if k else None, usd=float(v))
                    for k, v in spend.by_person.items()
                ],
            ),
        )

    async def put_setting(
        caller: WebCaller,
        key: SettingKey,
        channel_id: str | None,
        value: str,
        expected: str,
        *,
        audit_as: str,
    ) -> None:
        """One Settings-group write through the core writer, as every surface makes it."""
        platform = (
            await platform_row(caller, channel_id) if channel_id is not None else await home(caller)
        )
        legacy = actor_of(caller, channel_id or WORKSPACE_ID, platform, audit_as)

        async def admin_for(action: DelegatedAction | None) -> bool:
            needed = "workspace.manage" if key == "cost_footer" else "channels.manage"
            return needed in caller.capabilities and is_admin(caller.role)

        actor = SettingsActor(
            tenant_id=caller.person.tenant_id,
            account_id=caller.person.account_id,
            surface="web",
            native_user_id=await native_user_id(caller, platform)
            if platform in CHAT_PLATFORMS
            else None,
            admin_for=admin_for,
            on_write=legacy.on_write,
        )
        level: SettingLevel = "channel" if channel_id is not None else "server"
        with channel_refusals():
            await change_setting(
                runtime.sessionmaker,
                settings,
                actor,
                key=key,
                level=level,
                value=value,
                place=SettingsPlace(platform, channel_id),
                expected=Expected(expected),
            )

    async def put_switch(
        caller: WebCaller, switch: Switch, channel_id: str | None, body: SwitchChange
    ) -> None:
        await put_setting(
            caller,
            switch,
            channel_id,
            body.mode or "inherit",
            body.expected or "inherit",
            audit_as=switch,
        )

    async def put_approvals(
        caller: WebCaller, channel_id: str | None, body: ApprovalsChange
    ) -> None:
        await put_setting(
            caller,
            "approvals",
            channel_id,
            body.mode or "inherit",
            body.expected or "inherit",
            audit_as="approvals",
        )

    @router.put("/api/channels/{channel_id}/approvals")
    async def put_channel_approvals(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, channel_id: str, body: ApprovalsChange
    ) -> ChangeResultJson:
        await put_approvals(caller, channel_id, body)
        return await changed(caller, channel_id)

    @router.put("/api/channels/{channel_id}/participation")
    async def put_participation(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, channel_id: str, body: SwitchChange
    ) -> ChangeResultJson:
        await put_switch(caller, "participation", channel_id, body)
        return await changed(caller, channel_id)

    @router.put("/api/channels/{channel_id}/progress-plans")
    async def put_plans(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, channel_id: str, body: SwitchChange
    ) -> ChangeResultJson:
        await put_switch(caller, "progress_plans", channel_id, body)
        return await changed(caller, channel_id)

    async def put_env(caller: WebCaller, channel_id: str | None, body: EnvironmentChange) -> None:
        platform = await platform_row(caller, channel_id) if channel_id else await home(caller)
        with channel_refusals():
            await set_environment(
                client(),
                runtime.sessionmaker,
                actor_of(caller, channel_id or WORKSPACE_ID, platform, "environment"),
                platform=platform,
                channel_id=channel_id,
                environment_name=body.environment,
                default=runtime.deployment_default,
                confirm_open_network=body.confirm_open_network,
                expected=Expected(body.expected),
            )

    @router.put("/api/channels/{channel_id}/environment")
    async def put_environment(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, channel_id: str, body: EnvironmentChange
    ) -> ChangeResultJson:
        await put_env(caller, channel_id, body)
        return await changed(caller, channel_id)

    @router.post("/api/channels/{channel_id}/skills")
    async def post_skill(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, channel_id: str, body: SkillChange
    ) -> ChangeResultJson:
        platform = await platform_row(caller, channel_id)
        actor = actor_of(caller, channel_id, platform, "skills")
        with channel_refusals():
            if body.action == "remove":
                await remove_skill(
                    runtime.sessionmaker,
                    actor,
                    platform=platform,
                    channel_id=channel_id,
                    skill_id=body.skill,
                    expected=Expected(tuple(body.expected)),
                )
            else:
                try:
                    await add_skill(
                        client(),
                        runtime.sessionmaker,
                        actor,
                        platform=platform,
                        channel_id=channel_id,
                        skill=body.skill,
                        default=runtime.deployment_default,
                        platform_skills=load_platform_skills(
                            settings.defaults_root, features=settings.configured_features
                        ),
                        expected=Expected(tuple(body.expected)),
                    )
                except SkillsListTruncatedError as exc:
                    raise HTTPException(
                        503, "The skills could not all be read. Try again."
                    ) from exc
        return await changed(caller, channel_id)

    @router.put("/api/channels/{channel_id}/tool-rules")
    async def put_tool_rule(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, channel_id: str, body: ToolRuleChange
    ) -> ChangeResultJson:
        platform = await platform_row(caller, channel_id)
        agent = await agent_named(caller, body.agent)
        with channel_refusals():
            await set_tool_rule(
                runtime.sessionmaker,
                actor_of(caller, channel_id, platform, "tool_rule"),
                tool_safety=tool_safety(),
                channel_id=channel_id,
                agent=agent,
                tool_key=body.tool,
                rule=body.rule,
                scope=body.scope,
                expected_revision=body.expected_revision,
            )
        return await changed(caller, channel_id)

    @router.post("/api/channels/{channel_id}/provider-pin/clear")
    async def post_pin_clear(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, channel_id: str, body: PinClear
    ) -> ChangeResultJson:
        platform = await platform_row(caller, channel_id)
        with channel_refusals():
            await clear_provider_pin(
                runtime.sessionmaker,
                actor_of(caller, channel_id, platform, "provider_pin"),
                platform=platform,
                channel_id=channel_id,
                expected_local=body.expected_local,
            )
        return await changed(caller, channel_id)

    @router.post("/api/channels/{channel_id}/channel-copy/retire")
    async def post_copy_retire(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, channel_id: str, body: CopyRetire
    ) -> ChangeResultJson:
        platform = await platform_row(caller, channel_id)
        with channel_refusals():
            await retire_channel_copy(
                client(),
                runtime.sessionmaker,
                actor_of(caller, channel_id, platform, "channel_copy"),
                platform=platform,
                channel_id=channel_id,
                name=body.name,
                default=runtime.deployment_default,
                expected_ma_agent_id=body.agent_id,
            )
        return await changed(caller, channel_id)

    @router.post("/api/channels/{channel_id}/threads/clear")
    async def post_thread_clear(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, channel_id: str, body: ThreadClear
    ) -> ChangeResultJson:
        platform = await platform_row(caller, channel_id)
        with channel_refusals():
            await clear_thread_override(
                runtime.sessionmaker,
                actor_of(caller, channel_id, platform, "thread"),
                platform=platform,
                channel_id=channel_id,
                thread_id=body.thread_id,
                kind=body.kind,
            )
        return await changed(caller, channel_id)

    # ---------- Workspace defaults ----------

    async def home(caller: WebCaller) -> str:
        async with runtime.sessionmaker() as db:
            tenant = await get_tenant(db, caller.person.tenant_id)
        if tenant is None:
            raise HTTPException(404, "No such workspace.")
        return tenant.platform

    async def defaults(caller: WebCaller) -> DefaultsJson:
        tenant_id = caller.person.tenant_id
        platform = await home(caller)
        async with runtime.sessionmaker() as db:
            found = await load_channel_directory(
                db,
                tenant_id=tenant_id,
                default=runtime.deployment_default,
                now=datetime.now(UTC),
                markup=settings.billing.markup,
            )
            participation = await switch_json(db, "participation", tenant_id, platform, None)
            plans = await switch_json(db, "progress_plans", tenant_id, platform, None)
        approvals = await approvals_json(caller, tenant_id, platform, None)
        (footer,) = await read_settings(
            runtime.sessionmaker,
            tenant_id=tenant_id,
            place=SettingsPlace(platform, None),
            defaults=SettingsDefaults.from_settings(settings, platform),
            keys=("cost_footer",),
        )
        return DefaultsJson(
            platform=platform,
            agent=SourcedValueJson(
                value=found.default_agent.value, source=found.default_agent.source
            ),
            environment=SourcedValueJson(
                value=found.default_environment.value, source=found.default_environment.source
            ),
            participation=participation,
            progress_plans=plans,
            approvals=approvals,
            cost_footer=CostFooterJson(
                shown=footer.effective == "shown",
                editable="workspace.manage" in caller.capabilities,
            ),
        )

    @router.get("/api/channel-defaults")
    async def get_defaults(caller: ChannelViewer) -> DefaultsJson:  # pyright: ignore[reportUnusedFunction]
        return await defaults(caller)

    @router.put("/api/channel-defaults/agent")
    async def put_default_agent(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, body: DefaultAgentChange
    ) -> DefaultsJson:
        platform = await home(caller)
        names: tuple[str, ...] = ()
        if body.agent is not None:
            agent = await agent_named(caller, body.agent)
            names = tuple(n for n in agent_pin_names(agent.name, agent.metadata) if n)
        with channel_refusals():
            await set_workspace_default_agent(
                runtime.sessionmaker,
                actor_of(caller, WORKSPACE_ID, platform, "default_agent"),
                platform=platform,
                agent_name=body.agent,
                agent_names=names,
                expected=Expected(body.expected),
            )
        return await defaults(caller)

    @router.put("/api/channel-defaults/environment")
    async def put_default_environment(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, body: EnvironmentChange
    ) -> DefaultsJson:
        await put_env(caller, None, body)
        return await defaults(caller)

    @router.put("/api/channel-defaults/participation")
    async def put_default_participation(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, body: SwitchChange
    ) -> DefaultsJson:
        await put_switch(caller, "participation", None, body)
        return await defaults(caller)

    @router.put("/api/channel-defaults/progress-plans")
    async def put_default_plans(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, body: SwitchChange
    ) -> DefaultsJson:
        await put_switch(caller, "progress_plans", None, body)
        return await defaults(caller)

    @router.put("/api/channel-defaults/approvals")
    async def put_default_approvals(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, body: ApprovalsChange
    ) -> DefaultsJson:
        await put_approvals(caller, None, body)
        return await defaults(caller)

    @router.put("/api/channel-defaults/cost-footer")
    async def put_default_cost_footer(  # pyright: ignore[reportUnusedFunction]
        caller: WorkspaceAdmin, body: CostFooterChange
    ) -> DefaultsJson:
        await put_setting(
            caller,
            "cost_footer",
            None,
            "shown" if body.shown else "hidden",
            "shown" if body.expected else "hidden",
            audit_as="cost_footer",
        )
        return await defaults(caller)

    return router
