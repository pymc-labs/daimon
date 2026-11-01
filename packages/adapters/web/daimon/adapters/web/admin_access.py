"""`/api/admin/agent-access`: which agents the web chat offers, and to whom."""

from __future__ import annotations

import uuid
from typing import Any

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.admin_common import WorkspaceAdmin, listed_agents
from daimon.adapters.web.audit import audit
from daimon.adapters.web.channel_names import channel_names
from daimon.adapters.web.chat_json import agent_json
from daimon.adapters.web.faces import avatars_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.authz import build_agent_ref
from daimon.core.permissions import agent_permissions
from daimon.core.stores import web_agent_grants
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import WebAgentGrantRow, WebAgentWho
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


class AccessChange(BaseModel):
    web: bool | None = None
    who: WebAgentWho | None = None
    groups: list[str] | None = Field(default=None, max_length=100)


def access_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/admin")

    async def access_rows(tenant_id: uuid.UUID) -> list[dict[str, Any]]:
        agents = await listed_agents(runtime, tenant_id)
        async with runtime.sessionmaker() as db:
            policy = await load_access_policy(db, tenant_id=tenant_id)
            grants = await web_agent_grants.list_grants(db, tenant_id=tenant_id)
        avatars = await avatars_of(runtime, tenant_id, agents)
        fixed_to: dict[str, str] = {}
        for agent in agents:
            permissions = agent_permissions(
                policy, build_agent_ref(agent.name, agent.metadata).names
            )
            channels = sorted(c for rule in permissions.runs_in for c in rule)
            if fixed := permissions.home or (channels[0] if channels else None):
                fixed_to[agent.id] = fixed
        _, names = await channel_names(runtime, tenant_id, set(fixed_to.values()))
        return [
            _access_json(
                agent,
                grants.get(agent.id),
                fixed_to.get(agent.id),
                names,
                avatars.get(agent.id, ""),
            )
            for agent in agents
        ]

    @router.get("/agent-access")
    async def agent_access(caller: WorkspaceAdmin) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        return await access_rows(caller.person.tenant_id)

    @router.patch("/agent-access/{agent_id}")
    async def change_access(  # pyright: ignore[reportUnusedFunction]
        agent_id: str, body: AccessChange, caller: WorkspaceAdmin
    ) -> dict[str, Any]:
        tenant_id, now = caller.person.tenant_id, runtime.now()
        current = next(
            (r for r in await access_rows(tenant_id) if r["agent"]["id"] == agent_id), None
        )
        if current is None:
            raise HTTPException(status_code=404, detail="This agent isn't in the workspace.")
        if current["fixed_to"] is not None:
            raise HTTPException(status_code=403, detail="This agent is kept to its channel.")
        web = current["web"] if body.web is None else body.web
        who: WebAgentWho = current["who"] if body.who is None else body.who
        groups: list[str] = current["groups"] if body.groups is None else body.groups
        name = current["agent"]["name"]
        async with runtime.sessionmaker.begin() as db:
            await web_agent_grants.set_grant(
                db, tenant_id=tenant_id, agent_id=agent_id, web=web, who=who, groups=groups, now=now
            )
            if web != current["web"]:
                action = "Turned on web chat" if web else "Turned off web chat"
                await audit(
                    db,
                    tenant_id=tenant_id,
                    actor_account_id=caller.person.account_id,
                    action=action,
                    target=name,
                    at=now,
                )
            if (who, sorted(set(groups))) != (current["who"], sorted(current["groups"])):
                detail = {"everyone": "Everyone", "invites": "Invited people"}.get(who)
                await audit(
                    db,
                    tenant_id=tenant_id,
                    actor_account_id=caller.person.account_id,
                    action="Changed web access",
                    target=name,
                    detail=detail or ", ".join(groups) or "No groups",
                    at=now,
                )
        return {**current, "web": web, "who": who, "groups": list(dict.fromkeys(groups))}

    return router


def _access_json(
    agent: BetaManagedAgentsAgent,
    grant: WebAgentGrantRow | None,
    fixed_to: str | None,
    names: dict[str, str],
    avatar: str,
) -> dict[str, Any]:
    return {
        "agent": agent_json(agent, avatar=avatar),
        "web": fixed_to is None and (grant is None or grant.web),
        "who": "everyone" if grant is None else grant.who,
        "groups": [] if grant is None else list(grant.groups),
        "fixed_to": fixed_to,
        "fixed_name": None if fixed_to is None else names.get(fixed_to, "Unnamed channel"),
    }
