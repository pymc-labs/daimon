"""`/api/admin/dm*` and `/api/admin/platform-groups`: who a direct message reaches.

The web twin of `daimon dm default | rule | explain`. Writes go through
`daimon.core.dm_settings`, as chat's do; per-agent grants are on agent access.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Final

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.admin_common import WorkspaceAdmin, listed_agents
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import dm_settings
from daimon.core.authz import build_subject
from daimon.core.dm_access import DmAgent, decide_dm, dm_agents_of, group_ids_of, load_routing
from daimon.core.dm_routing import (
    DmChoose,
    DmDecision,
    DmRefused,
    DmRuleFact,
    DmStart,
    DmUnchecked,
)
from daimon.core.dm_settings import MAX_SUBJECTS, DmActor, DmRuleSpec, PlatformId, dm_platform
from daimon.core.errors import UserFacingError
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.github_credentials import decrypt_token
from daimon.core.native_admins import GroupLookupFailed
from daimon.core.platform_groups import (
    PlatformGroup,
    discord_roles,
    merge_groups,
    slack_user_groups,
)
from daimon.core.stores import dm_access as store
from daimon.core.stores import teams_installations
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.direct_messages import get_dm_agent_name
from daimon.core.stores.domain import DmGrantMode, DmRuleRow, DmRuleSubject
from daimon.core.stores.platform_names import KnownName, get_user_names, search_user_names
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from daimon.core.stores.tenants import get_tenant
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

NO_DMS: Final = "This workspace has no direct messages."
NO_RULE: Final = "No such rule."

DmWrite = Callable[[AsyncSession, DmActor, list[DmAgent]], Awaitable[object]]
"""One settings change; returning False means there was no such rule."""


class DmChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_agent: str | None


class DmAccessChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    who: DmGrantMode
    user_ids: list[PlatformId] = Field(default_factory=list[str], max_length=MAX_SUBJECTS)
    group_ids: list[PlatformId] = Field(default_factory=list[str], max_length=MAX_SUBJECTS)


class RuleAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: DmRuleSubject
    subject_id: PlatformId | None = None
    agent: str | None = None
    locked: bool = False
    position: int | None = Field(default=None, ge=0)


class RuleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str | None = None
    locked: bool | None = None
    to: int | None = Field(default=None, ge=0)


def _actor(runtime: WebRuntime, caller: WebCaller) -> DmActor:
    return DmActor(caller.person.tenant_id, caller.person.account_id, runtime.now())


async def dm_access_of(
    runtime: WebRuntime,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    agents: list[BetaManagedAgentsAgent],
) -> dict[str, dict[str, Any]]:
    """Each DM agent's switch and grant by agent id; empty when the workspace has no DMs."""
    platform = await dm_platform(db, tenant_id)
    if platform is None:
        return {}
    grants = await store.list_grants(db, tenant_id=tenant_id)
    names = await get_user_names(
        db,
        tenant_id=tenant_id,
        platform=platform,
        user_ids=sorted({u for g in grants.values() for u in g.user_ids}),
    )
    found: dict[str, dict[str, Any]] = {}
    for agent in dm_agents_of(agents):
        place = FeaturePlace(tenant_id, platform, agent_id=agent.ma_agent_id)
        grant = grants.get(agent.name)
        found[agent.ma_agent_id] = {
            "on": (await feature_in(db, runtime.settings, place, "direct_messages")).on,
            "who": "members" if grant is None else grant.who,
            "users": [_named(u, names) for u in (() if grant is None else grant.user_ids)],
            "groups": [] if grant is None else list(grant.group_ids),
        }
    return found


async def change_dm_access(
    runtime: WebRuntime,
    db: AsyncSession,
    caller: WebCaller,
    agent: BetaManagedAgentsAgent,
    change: DmAccessChange,
) -> None:
    """Replace who may DM `agent`."""
    if await dm_platform(db, caller.person.tenant_id) is None:
        raise HTTPException(status_code=409, detail=NO_DMS)
    dm_agents = dm_agents_of([agent])
    if not dm_agents:
        raise HTTPException(status_code=409, detail="This agent doesn't take direct messages.")
    await dm_settings.set_grant(
        db,
        _actor(runtime, caller),
        dm_agents[0],
        agent.name,
        who=change.who,
        user_ids=change.user_ids,
        group_ids=change.group_ids,
    )


def dm_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/admin")

    async def agents_of(tenant_id: uuid.UUID) -> list[DmAgent]:
        return dm_agents_of(await listed_agents(runtime, tenant_id))

    async def platform_of(tenant_id: uuid.UUID) -> str:
        async with runtime.sessionmaker() as db:
            platform = await dm_platform(db, tenant_id)
        if platform is None:
            raise HTTPException(status_code=409, detail=NO_DMS)
        return platform

    async def doc(tenant_id: uuid.UUID) -> dict[str, Any]:
        agents = await agents_of(tenant_id)
        async with runtime.sessionmaker() as db:
            platform = await dm_platform(db, tenant_id)
            default = await get_dm_agent_name(db, tenant_id=tenant_id)
            rules = await store.list_rules(db, tenant_id=tenant_id)
            names = await get_user_names(
                db,
                tenant_id=tenant_id,
                platform=platform or "",
                user_ids=[r.subject_id for r in rules if r.subject == "user" and r.subject_id],
            )
        return {
            "platform": platform,
            "default_agent": default,
            "agents": [{"id": a.ma_agent_id, "name": a.name} for a in agents],
            "rules": [_rule_json(r, names) for r in rules],
        }

    async def write(caller: WebCaller, change: DmWrite) -> dict[str, Any]:
        """Run one change in a transaction; a refusal is a 422, a missing rule a 404."""
        tenant_id = caller.person.tenant_id
        await platform_of(tenant_id)
        agents = await agents_of(tenant_id)
        try:
            async with runtime.sessionmaker.begin() as db:
                if await change(db, _actor(runtime, caller), agents) is False:
                    raise HTTPException(status_code=404, detail=NO_RULE)
        except UserFacingError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return await doc(tenant_id)

    @router.get("/dm")
    async def read(caller: WorkspaceAdmin) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return await doc(caller.person.tenant_id)

    @router.patch("/dm")
    async def change(body: DmChange, caller: WorkspaceAdmin) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        async def run(db: AsyncSession, actor: DmActor, agents: list[DmAgent]) -> None:
            await dm_settings.set_default(db, actor, agents, body.default_agent)

        return await write(caller, run)

    @router.post("/dm/rules")
    async def add_rule(body: RuleAdd, caller: WorkspaceAdmin) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        rule = DmRuleSpec(body.subject, body.subject_id, body.agent, body.locked)

        async def run(db: AsyncSession, actor: DmActor, agents: list[DmAgent]) -> None:
            await dm_settings.add_rule(db, actor, agents, rule, body.position)

        return await write(caller, run)

    @router.patch("/dm/rules/{position}")
    async def change_rule(  # pyright: ignore[reportUnusedFunction]
        position: int, body: RuleChange, caller: WorkspaceAdmin
    ) -> dict[str, Any]:
        async def run(db: AsyncSession, actor: DmActor, agents: list[DmAgent]) -> bool:
            rules = await store.list_rules(db, tenant_id=actor.tenant_id)
            if not 0 <= position < len(rules):
                return False
            rule = rules[position]
            return await dm_settings.change_rule(
                db,
                actor,
                agents,
                position,
                agent=body.agent if "agent" in body.model_fields_set else rule.agent_name,
                locked=rule.locked if body.locked is None else body.locked,
                to=body.to,
            )

        return await write(caller, run)

    @router.delete("/dm/rules/{position}")
    async def remove_rule(position: int, caller: WorkspaceAdmin) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        async def run(db: AsyncSession, actor: DmActor, agents: list[DmAgent]) -> bool:
            return await dm_settings.remove_rule(db, actor, position)

        return await write(caller, run)

    @router.get("/dm/explain")
    async def explain(  # pyright: ignore[reportUnusedFunction]
        caller: WorkspaceAdmin,
        user: Annotated[PlatformId, Query()],
        group: Annotated[list[PlatformId] | None, Query(max_length=MAX_SUBJECTS)] = None,
    ) -> dict[str, Any]:
        tenant_id = caller.person.tenant_id
        platform = await platform_of(tenant_id)
        agents = await agents_of(tenant_id)
        async with runtime.sessionmaker() as db:
            policy = await load_access_policy(db, tenant_id=tenant_id)
            subject = build_subject(
                is_admin=False,
                platform_user_id=user,
                role_ids=tuple(group or ()),
                verified_membership=True,
            )
            decision = await decide_dm(
                db,
                runtime.settings,
                policy,
                tenant_id=tenant_id,
                platform=platform,
                agents=agents,
                subject=subject,
                account_id=None,
            )
            rules = (await load_routing(db, tenant_id=tenant_id)).rules
        return explain_json(decision, rules)

    @router.get("/dm/people")
    async def people(  # pyright: ignore[reportUnusedFunction]
        caller: WorkspaceAdmin, q: Annotated[str, Query(max_length=100)] = ""
    ) -> list[dict[str, Any]]:
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            platform = await dm_platform(db, tenant_id)
            if platform is None or not q.strip():
                return []
            found = await search_user_names(
                db, tenant_id=tenant_id, platform=platform, text=q.strip(), limit=8
            )
        return [_named(user_id, found) for user_id in found]

    @router.get("/platform-groups")
    async def platform_groups(caller: WorkspaceAdmin) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            tenant = await get_tenant(db, tenant_id)
            platform = await dm_platform(db, tenant_id)
            routing = await load_routing(db, tenant_id=tenant_id)
            slack = (
                await get_slack_bot_token(db, team_id=tenant.external_id)
                if tenant is not None and platform == "slack"
                else None
            )
            teams = (
                await teams_installations.list_teams_installations(db, tenant_id=tenant_id)
                if platform == "teams"
                else []
            )
        live: tuple[PlatformGroup, ...] = ()
        problem: str | None = None
        discord = runtime.settings.discord
        try:
            if platform == "teams":
                live = tuple(PlatformGroup(t.group_id, t.name) for t in teams)
            elif platform == "slack" and slack is not None:
                token = decrypt_token(runtime.fernet, slack.encrypted_token)
                live = await slack_user_groups(runtime.http_client, token=token)
            elif platform == "discord" and tenant is not None and discord is not None:
                live = await discord_roles(
                    runtime.http_client,
                    token=discord.bot_token.get_secret_value(),
                    guild_id=tenant.external_id,
                )
            elif platform is not None:
                problem = "unavailable"
        except GroupLookupFailed as error:
            problem = "reinstall" if str(error) == "missing_scope" else "unavailable"
        groups = merge_groups(live, group_ids_of(routing))
        return {
            "platform": platform,
            "groups": [{"id": g.id, "name": g.name} for g in groups],
            "problem": problem,
        }

    return router


def explain_json(decision: DmDecision, rules: list[DmRuleFact]) -> dict[str, Any]:
    """What `decide_dm` chose, for the "Check a person" box; `rule` is a 0-based position."""
    route = decision.route
    match route:
        case DmStart():
            kind, agent, reason, agents = "start", route.agent, route.reason, ()
        case DmChoose():
            kind, agent, reason, agents = "choose", None, None, route.agents
        case DmRefused():
            kind, agent, reason, agents = "refused", None, route.reason, ()
        case DmUnchecked():
            kind, agent, reason, agents = "unchecked", None, None, ()
    rule = decision.rule
    return {
        "route": kind,
        "agent": agent,
        "reason": reason,
        "agents": list(agents),
        "allowed": list(decision.allowed),
        "rule": None if rule is None else rules.index(rule),
    }


def _named(user_id: str, names: dict[str, KnownName]) -> dict[str, str | None]:
    known = names.get(user_id)
    return {"id": user_id, "name": None if known is None else known.label}


def _rule_json(rule: DmRuleRow, names: dict[str, KnownName]) -> dict[str, Any]:
    return {
        "position": rule.position,
        "subject": rule.subject,
        "subject_id": rule.subject_id,
        "name": _named(rule.subject_id, names)["name"] if rule.subject_id else None,
        "agent": rule.agent_name,
        "locked": rule.locked,
    }
