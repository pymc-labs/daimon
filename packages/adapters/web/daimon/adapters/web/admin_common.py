"""What the `/api/admin/*` routers share: who may call them, and the lookups they repeat."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.access import Capability, Role
from daimon.adapters.web.caller import NOT_ALLOWED, WebCaller, current_caller, requires
from daimon.adapters.web.me import Identity
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import WebInviteRow, WebPersonRow, WebPersonStatus
from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

PeopleAdmin = Annotated[WebCaller, Depends(requires("people.manage"))]
AuditViewer = Annotated[WebCaller, Depends(requires("audit.view"))]
WorkspaceAdmin = Annotated[WebCaller, Depends(requires("workspace.manage"))]

_PICKING = frozenset[Capability]({"people.manage", "credits.manage", "workspace.manage"})


async def _picker(caller: Annotated[WebCaller, Depends(current_caller)]) -> WebCaller:
    if not caller.capabilities & _PICKING:
        raise HTTPException(status_code=403, detail=NOT_ALLOWED)
    return caller


# Whoever fills a people or group picker: the credits, agent access and people pages each
# do, and each may be the only admin page the workspace left on.
Pickers = Annotated[WebCaller, Depends(_picker)]

ROLE_WORDS: dict[Role, str] = {
    "owner": "Owner",
    "admin": "Admin",
    "member": "Member",
    "visitor": "Visitor",
}


class Credit(BaseModel):
    left_usd: float
    total_usd: float


class AdminPerson(BaseModel):
    id: str
    name: str
    email: str
    role: Role
    status: WebPersonStatus
    groups: list[str]
    identities: list[Identity]
    credit: Credit | None
    last_seen: str | None


def no_person() -> HTTPException:
    return HTTPException(status_code=404, detail="This person isn't in the workspace.")


def conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


async def person_of(db: AsyncSession, caller: WebCaller, person_id: uuid.UUID) -> WebPersonRow:
    person = await people_store.get_person(
        db, tenant_id=caller.person.tenant_id, person_id=person_id
    )
    if person is None:
        raise no_person()
    return person


def invite_status(invite: WebInviteRow, now: datetime) -> Literal["pending", "accepted", "expired"]:
    if invite.accepted_at is not None:
        return "accepted"
    return "expired" if invite.expires_at <= now else "pending"


async def listed_agents(runtime: WebRuntime, tenant_id: uuid.UUID) -> list[BetaManagedAgentsAgent]:
    """The workspace's agents, through the same cache the chat reads."""
    if runtime.turns is None:
        return []
    return await runtime.agents.agents(runtime.turns.deps.anthropic, tenant_id)


async def names_by_account(db: AsyncSession, tenant_id: uuid.UUID) -> dict[uuid.UUID, str]:
    people = await people_store.list_people(db, tenant_id=tenant_id)
    return {p.account_id: p.display_name for p in people}
