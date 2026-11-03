"""`/api/agents/{id}/owners`: who owns an agent, changed by admins."""

from __future__ import annotations

import uuid

from daimon.adapters.web.agent_access import Editor, actor_of, change_deps, edit_refusal
from daimon.adapters.web.agent_audit import recorded
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import agent_admin
from daimon.core.agent_editing import AgentEditError
from daimon.core.agent_view import PersonRef
from daimon.core.stores import web_people as people_store
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


class OwnersBody(BaseModel):
    """The people who should own the agent. An id is a web person's, else an account's."""

    owner: str | None
    co_owners: list[str] = Field(default_factory=list)


def agent_people_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/agents")

    @router.get("/{agent_id}/owners/options")
    async def options(caller: Editor, agent_id: str) -> list[PersonRef]:  # pyright: ignore[reportUnusedFunction]
        try:
            agent_admin.require_admin(actor_of(caller), "Changing owners")
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        async with runtime.sessionmaker() as db:
            people = await people_store.list_people(db, tenant_id=caller.person.tenant_id)
        return [
            PersonRef(id=str(p.id), name=p.display_name)
            for p in people
            if p.kind == "member" and p.status == "active"
        ]

    @router.put("/{agent_id}/owners", status_code=204)
    async def set_owners(caller: Editor, agent_id: str, body: OwnersBody) -> None:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker() as db:
            people = await people_store.list_people(db, tenant_id=caller.person.tenant_id)
        accounts = {str(p.id): p.account_id for p in people}

        def account_of(ref: str) -> uuid.UUID:
            if ref in accounts:
                return accounts[ref]
            try:
                return uuid.UUID(ref)
            except ValueError:
                raise HTTPException(status_code=422, detail="That person is not in this workspace.")  # noqa: B904

        owner = None if body.owner is None else account_of(body.owner)
        try:
            await agent_admin.set_owners(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                owner_account_id=owner,
                co_owner_account_ids=[account_of(c) for c in body.co_owners],
                audited=recorded(runtime, caller, "Changed agent owners"),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    return router
