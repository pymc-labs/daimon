"""`/api/admin/agent-access`: which agents the web chat offers, and to whom."""

from __future__ import annotations

import uuid
from typing import Any

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.admin_common import WorkspaceAdmin, admin_of, listed_agents, refused
from daimon.adapters.web.channel_names import channel_names
from daimon.adapters.web.chat_json import agent_json
from daimon.adapters.web.faces import avatars_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import web_admin
from daimon.core.platform_names import UNNAMED_CHANNEL
from daimon.core.stores.domain import WebAgentWho
from daimon.core.web_admin import WebAdminError
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
            found = await web_admin.agent_access_rows(db, tenant_id, agents)
        avatars = await avatars_of(runtime, tenant_id, agents)
        _, names_found = await channel_names(
            runtime, tenant_id, {r.fixed_to for r in found if r.fixed_to}
        )
        names = names_found.labels()
        by_id = {a.id: a for a in agents}
        return [
            _access_json(by_id[r.agent_id], r, names, avatars.get(r.agent_id, "")) for r in found
        ]

    @router.get("/agent-access")
    async def agent_access(caller: WorkspaceAdmin) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        return await access_rows(caller.person.tenant_id)

    @router.patch("/agent-access/{agent_id}")
    async def change_access(  # pyright: ignore[reportUnusedFunction]
        agent_id: str, body: AccessChange, caller: WorkspaceAdmin
    ) -> dict[str, Any]:
        tenant_id = caller.person.tenant_id
        agents = [a for a in await listed_agents(runtime, tenant_id) if a.id == agent_id]
        if not agents:
            raise HTTPException(status_code=404, detail="This agent isn't in the workspace.")
        try:
            async with runtime.sessionmaker.begin() as db:
                [current] = await web_admin.agent_access_rows(db, tenant_id, agents)
                changed = await web_admin.set_agent_access(
                    db,
                    admin_of(caller),
                    runtime.now(),
                    current,
                    web=body.web,
                    who=body.who,
                    groups=body.groups,
                )
        except WebAdminError as error:
            raise refused(error) from error
        return {
            "agent": agent_json(
                agents[0], avatar=(await avatars_of(runtime, tenant_id, agents)).get(agent_id, "")
            ),
            "web": changed.web,
            "who": changed.who,
            "groups": changed.groups,
            "fixed_to": changed.fixed_to,
            "fixed_name": None,
        }

    return router


def _access_json(
    agent: BetaManagedAgentsAgent,
    row: web_admin.AgentAccess,
    names: dict[str, str],
    avatar: str,
) -> dict[str, Any]:
    return {
        "agent": agent_json(agent, avatar=avatar),
        "web": row.web,
        "who": row.who,
        "groups": row.groups,
        "fixed_to": row.fixed_to,
        "fixed_name": None if row.fixed_to is None else names.get(row.fixed_to, UNNAMED_CHANNEL),
    }
