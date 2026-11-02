"""`/api/admin/invites`: who has been invited to the workspace, and by whom."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.admin_common import (
    ROLE_WORDS,
    PeopleAdmin,
    invite_status,
    listed_agents,
    names_by_account,
)
from daimon.adapters.web.audit import audit, invite_target, usd
from daimon.adapters.web.chat_json import agent_ref
from daimon.adapters.web.faces import avatars_of
from daimon.adapters.web.invites import issue_link, link_url
from daimon.adapters.web.paging import PageRequest, cut, page_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.stores import web_invites
from daimon.core.stores.domain import WebInviteRow
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, EmailStr, Field


class NewInvite(BaseModel):
    email: EmailStr
    role: Literal["visitor", "member"]
    agents: list[str] = Field(default_factory=list[str], max_length=50)
    credit_usd: Decimal = Field(ge=0, le=100_000, decimal_places=2)
    days: int = Field(ge=1, le=90)


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
        known = {a.id for a in agents}
        picked = list(dict.fromkeys(body.agents))
        if any(a not in known for a in picked):
            raise HTTPException(status_code=422, detail="Pick agents from this workspace.")
        if body.role == "visitor" and not picked:
            raise HTTPException(status_code=422, detail="Pick at least one agent.")
        async with runtime.sessionmaker.begin() as db:
            invite, token = await issue_link(
                db,
                tenant_id=tenant_id,
                kind="invite",
                expires_at=now + timedelta(days=body.days),
                email=str(body.email),
                role=body.role,
                credit_usd=body.credit_usd,
                agent_ids=picked,
                created_by_account_id=caller.person.account_id,
            )
            names = [
                agent_ref(next((x for x in agents if x.id == a), None), a).name for a in picked
            ]
            await audit(
                db,
                tenant_id=tenant_id,
                actor_account_id=caller.person.account_id,
                action="Created invite",
                target=invite_target(invite.id),
                detail=", ".join([ROLE_WORDS[body.role], *names, usd(body.credit_usd)]),
                at=now,
            )
            people_names = await names_by_account(db, tenant_id)
        return invite_json(
            invite,
            agents,
            people_names,
            await avatars_of(runtime, tenant_id, agents),
            link=link_url(runtime.web, token),
        )

    @router.delete("/invites/{invite_id}")
    async def revoke_invite(invite_id: uuid.UUID, caller: PeopleAdmin) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        gone = HTTPException(status_code=404, detail="This invite no longer exists.")
        now = runtime.now()
        async with runtime.sessionmaker.begin() as db:
            invite = await web_invites.revoke_invite(
                db,
                tenant_id=caller.person.tenant_id,
                invite_id=invite_id,
                now=now,
            )
            if invite is None:
                raise gone
            await audit(
                db,
                tenant_id=invite.tenant_id,
                actor_account_id=caller.person.account_id,
                action="Revoked invite",
                target=invite_target(invite.id),
                at=now,
            )
        return {"ok": True}

    return router
