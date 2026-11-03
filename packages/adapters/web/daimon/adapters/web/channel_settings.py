"""`/api/channels/{id}/settings` and its writes, and `/api/channel-defaults`.

A channel's further settings (following threads, progress plans, sandbox, skills,
tool rules, provider pin, its copy, its threads' own settings) and what its page
reads beyond the row (spend three ways, routines, link, Teams subscription). Every
write goes through `daimon.core.channel_settings` or `channel_switches` with the
value the admin last read; members read only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.access import is_admin
from daimon.adapters.web.admin_common import listed_agents
from daimon.adapters.web.caller import WebCaller, requires
from daimon.adapters.web.channels import (
    ChangeResultJson,
    actor_of,
    channel_refusals,
    detail_json,
    one_row,
)
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.agent_pins import agent_pin_names
from daimon.core.channel_changes import Expected
from daimon.core.channel_directory import WEB_ROW_ID, ValueSource, load_channel_directory
from daimon.core.channel_reads import channel_routines, channel_spend, platform_link
from daimon.core.channel_reads import teams_subscription as read_subscription
from daimon.core.channel_settings import (
    add_skill,
    clear_provider_pin,
    clear_thread_override,
    list_thread_overrides,
    pin_json,
    provider_pin,
    remove_skill,
    retire_channel_copy,
    set_environment,
    set_tool_rule,
    set_workspace_default_agent,
)
from daimon.core.channel_switches import SWITCH_PLATFORMS, SwitchTarget, own_mode, set_switch
from daimon.core.credit_views import recent_start
from daimon.core.defaults.loader import load_platform_skills
from daimon.core.defaults.metadata import MA_METADATA_KEY_CHANNEL_COPY
from daimon.core.errors import SkillsListTruncatedError
from daimon.core.scope_cascade import CascadeMode, CascadeScope, resolve_modes
from daimon.core.stores import agent_tool_rules as tool_rules
from daimon.core.stores.channel_changes import WORKSPACE_ID
from daimon.core.stores.channel_skills import list_channel_skills
from daimon.core.stores.platform_names import get_user_names
from daimon.core.stores.switch_modes import Switch, get_switch_modes
from daimon.core.stores.tenants import get_tenant
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

ChannelViewer = Annotated[WebCaller, Depends(requires("channels.view"))]
ChannelAdmin = Annotated[WebCaller, Depends(requires("channels.manage"))]
Mode = Literal["on", "off", "disabled"]
_SPEND_DAYS = 30
_NO_WEB = "Web chat has no such setting."
_NO_AGENT = "Pick an agent from this workspace."


class SwitchJson(BaseModel):
    available: bool
    own: Mode | None
    """The channel's (or workspace's) own mode; None while it inherits."""
    value: Mode
    source: Literal["deployment", "workspace", "channel", "thread"]


class SkillJson(BaseModel):
    skill_id: str
    name: str


class ToolRuleJson(BaseModel):
    agent: str
    tool: str
    rule: Literal["allow", "ask", "deny"] | None
    scope: Literal["chat", "chat_and_routines"]
    revision: int


class ToolRulesJson(BaseModel):
    editable: bool
    rules: list[ToolRuleJson]


class PinJson(BaseModel):
    backend: str
    model: str | None
    local: int


class CopyJson(BaseModel):
    name: str
    agent_id: str


class ThreadJson(BaseModel):
    thread_id: str
    kind: Literal["participation", "progress_plans", "thread_rule", "handoff"]
    value: str


class SubscriptionJson(BaseModel):
    status: Literal["active", "expired", "missing"]
    expires_at: str | None


class RoutineJson(BaseModel):
    id: str
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
    skills: list[SkillJson]
    tool_rules: ToolRulesJson
    provider_pin: PinJson | None
    channel_copy: CopyJson | None
    threads: list[ThreadJson]
    link: str | None
    teams_subscription: SubscriptionJson | None
    routines: list[RoutineJson]
    spend: SpendBreakdownJson


class SourcedValueJson(BaseModel):
    value: str | None
    source: ValueSource | None


class DefaultsJson(BaseModel):
    platform: str
    agent: SourcedValueJson
    environment: SourcedValueJson
    participation: SwitchJson
    progress_plans: SwitchJson


class SwitchChange(BaseModel):
    mode: Mode | None
    expected: Mode | None


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


def _mode(mode: CascadeMode | None) -> Mode | None:
    return None if mode is None else mode.value


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
        return ChangeResultJson(channel=detail_json(row, manage=True), notes=[])

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
            name = row.name
            sub = await read_subscription(
                db,
                tenant_id=tenant_id,
                channel_id=channel_id,
                kind=name.kind if name else None,
                now=datetime.now(UTC),
            )
            routines = await channel_routines(db, tenant_id=tenant_id, channel_id=channel_id)
            spend = await channel_spend(
                db,
                tenant_id=tenant_id,
                channel_id=channel_id,
                since=recent_start(datetime.now(UTC)),
                markup=settings.billing.markup,
            )
            people = await get_user_names(
                db,
                tenant_id=tenant_id,
                platform=platform,
                user_ids=[p for p in spend.by_person if p is not None],
            )
            participation = await switch_json(db, "participation", tenant_id, platform, channel_id)
            plans = await switch_json(db, "progress_plans", tenant_id, platform, channel_id)
        copy = next(
            (a for a in listed if a.metadata.get(MA_METADATA_KEY_CHANNEL_COPY) == channel_id),
            None,
        )
        person_name = {k: v.display_name or v.handle for k, v in people.items()}
        return ChannelSettingsJson(
            participation=participation,
            progress_plans=plans,
            skills=[SkillJson(skill_id=s.skill_id, name=s.name) for s in skills],
            tool_rules=ToolRulesJson(
                editable=tool_safety() and is_admin(caller.role),
                rules=[
                    ToolRuleJson(
                        agent=r.agent_name,
                        tool=r.tool_key,
                        rule=r.rule,
                        scope=r.scope,
                        revision=r.revision,
                    )
                    for r in rules
                ],
            ),
            provider_pin=PinJson.model_validate(pin_json(pin)) if pin is not None else None,
            channel_copy=CopyJson(name=copy.name, agent_id=copy.id) if copy else None,
            threads=[
                ThreadJson(thread_id=t.thread_id, kind=t.kind, value=t.value) for t in threads
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
                    agent=r.agent_name,
                    cron=r.cron_expr,
                    enabled=r.enabled,
                    relation=rel,
                )
                for r, rel in routines
            ],
            spend=SpendBreakdownJson(
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

    async def put_switch(
        caller: WebCaller, switch: Switch, channel_id: str | None, body: SwitchChange
    ) -> None:
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            tenant = await get_tenant(db, tenant_id)
        platform = (
            await platform_row(caller, channel_id)
            if channel_id is not None
            else (tenant.platform if tenant else "")
        )
        target = (
            SwitchTarget.channel(channel_id) if channel_id is not None else SwitchTarget.workspace()
        )
        with channel_refusals():
            await set_switch(
                runtime.sessionmaker,
                actor_of(caller, channel_id or WORKSPACE_ID, platform, switch),
                switch,
                platform=platform,
                deployment=deployment(switch, platform),
                target=target,
                mode=None if body.mode is None else CascadeMode(body.mode),
                expected=Expected(None if body.expected is None else CascadeMode(body.expected)),
            )

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
                db, tenant_id=tenant_id, default=runtime.deployment_default, now=datetime.now(UTC)
            )
            participation = await switch_json(db, "participation", tenant_id, platform, None)
            plans = await switch_json(db, "progress_plans", tenant_id, platform, None)
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

    return router
