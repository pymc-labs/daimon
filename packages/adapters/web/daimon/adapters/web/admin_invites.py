"""`/api/admin/invites`: who has been invited to the workspace, and by whom."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Annotated, Any, Literal

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.admin_common import (
    PeopleAdmin,
    admin_of,
    invite_status,
    listed_agents,
    names_by_account,
    refused,
)
from daimon.adapters.web.chat_json import agent_ref
from daimon.adapters.web.faces import avatars_of
from daimon.adapters.web.paging import PageRequest, cut, page_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import web_admin
from daimon.core.stores import web_invites
from daimon.core.stores.domain import WebInviteRow
from daimon.core.web_admin import WebAdminError, link_url
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, EmailStr, Field


class NewInvite(BaseModel):
    email: EmailStr
    role: Literal["visitor", "member"]
    agents: list[str] = Field(default_factory=list[str], max_length=50)
    credit_usd: Decimal = Field(ge=0, le=100_000, decimal_places=2)
    days: int = Field(ge=1, le=90)
    # Visitors only; a member may always attach files.
    allow_files: bool = False


class InviteChange(BaseModel):
    allow_files: bool


def invites_admin_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/admin")

    def invite_json(
        invite: WebInviteRow,
        agents: Sequence[BetaManagedAgentsAgent],
        names: dict[uuid.UUID, str],
        avatars: dict[str, str],
        link: str | None = None,
    ) -> dict[str, Any]:
        by = names.get(invite.created_by_account_id) if invite.created_by_account_id else None
        out: dict[str, Any] = {
            "id": str(invite.id),
            "email": invite.email,
            "role": invite.role,
            "agents": [
                agent_ref(
                    next((x for x in agents if x.id == a), None), a, avatars.get(a, "")
                ).model_dump()
                for a in invite.agent_ids
            ],
            "credit_usd": float(invite.credit_usd),
            "allow_files": invite.allow_files,
            "expires_at": invite.expires_at.isoformat(),
            "status": invite_status(invite, runtime.now()),
            "by": by or "An admin",
        }
        if link is not None:
            out["link"] = link
        return out

    @router.get("/invites")
    async def invites(  # pyright: ignore[reportUnusedFunction]
        caller: PeopleAdmin,
        response: Response,
        page: Annotated[PageRequest, Depends(page_of(100))],
    ) -> list[dict[str, Any]]:
        tenant_id = caller.person.tenant_id
        agents = await listed_agents(runtime, tenant_id)
        async with runtime.sessionmaker() as db:
            found = await web_invites.list_invites(
                db, tenant_id=tenant_id, limit=page.limit + 1, before=page.cursor
            )
            names = await names_by_account(db, tenant_id)
        rows = cut(found, page, response, lambda r: (r.created_at, r.id))
        avatars = await avatars_of(runtime, tenant_id, agents)
        return [invite_json(r, agents, names, avatars) for r in rows]

    @router.post("/invites")
    async def create_invite(body: NewInvite, caller: PeopleAdmin) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        tenant_id, now = caller.person.tenant_id, runtime.now()
        agents = await listed_agents(runtime, tenant_id)
        try:
            async with runtime.sessionmaker.begin() as db:
                invite, token = await web_admin.create_invite(
                    db,
                    admin_of(caller),
                    now,
                    email=str(body.email),
                    role=body.role,
                    agent_ids=body.agents,
                    agents={a.id: agent_ref(a, a.id).name for a in agents},
                    credit_usd=body.credit_usd,
                    days=body.days,
                    allow_files=body.allow_files,
                )
                people_names = await names_by_account(db, tenant_id)
        except WebAdminError as error:
            raise refused(error) from error
        return invite_json(
            invite,
            agents,
            people_names,
            await avatars_of(runtime, tenant_id, agents),
            link=link_url(runtime.web, token),
        )

    @router.patch("/invites/{invite_id}")
    async def change_invite(  # pyright: ignore[reportUnusedFunction]
        invite_id: uuid.UUID, body: InviteChange, caller: PeopleAdmin
    ) -> dict[str, Any]:
        """Turn files on or off for a visitor invite, whether or not it was accepted."""
        tenant_id = caller.person.tenant_id
        try:
            async with runtime.sessionmaker.begin() as db:
                invite = await web_admin.set_invite_files(
                    db, admin_of(caller), runtime.now(), invite_id, allow=body.allow_files
                )
                names = await names_by_account(db, tenant_id)
        except WebAdminError as error:
            raise refused(error) from error
        agents = await listed_agents(runtime, tenant_id)
        return invite_json(invite, agents, names, await avatars_of(runtime, tenant_id, agents))

    @router.delete("/invites/{invite_id}")
    async def revoke_invite(invite_id: uuid.UUID, caller: PeopleAdmin) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        try:
            async with runtime.sessionmaker.begin() as db:
                await web_admin.revoke_invite(db, admin_of(caller), runtime.now(), invite_id)
        except WebAdminError as error:
            raise refused(error) from error
        return {"ok": True}

    return router
