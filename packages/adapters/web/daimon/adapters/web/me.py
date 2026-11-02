"""`/api/me` and workspace switching: who the app is showing, and where.

A workspace whose chat member list no longer has the person drops out of the list, and
the app moves them on from it.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from daimon.adapters.web.access import Capability, Role
from daimon.adapters.web.caller import (
    LEFT_WORKSPACE,
    WebCaller,
    caller_who_may_be_suspended,
    caller_who_may_have_left,
    chat_standings,
    current_caller,
    has_left,
    roles_of,
    signin_allowed_in,
)
from daimon.adapters.web.credits import CreditMeter, credit_meter
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.config import WebModuleKey
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores.domain import CHAT_PLATFORMS, Platform, WebIdentityRow, WebPersonRow
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field


def _omit_none(value: object) -> bool:
    return value is None


ShownPlatform = Literal["discord", "slack", "teams", "web"]


class Workspace(BaseModel):
    id: str
    name: str
    platform: ShownPlatform
    role: Role
    members: int


class Identity(BaseModel):
    provider: str
    provider_label: str
    label: str
    icon: str | None = Field(default=None, exclude_if=_omit_none)


class Me(BaseModel):
    """The shape the single-page app reads (`web/src/lib/types.ts`)."""

    id: str
    name: str
    email: str
    workspace: Workspace
    workspaces: list[Workspace]
    role: Role
    modules: list[WebModuleKey]
    capabilities: list[Capability]
    identities: list[Identity]
    credit: CreditMeter | None = None
    invited_by: str | None = Field(default=None, exclude_if=_omit_none)
    dev: bool


class SwitchBody(BaseModel):
    id: str


_PROVIDER_LABELS = {"discord": "Discord"}


def identity_json(row: WebIdentityRow) -> Identity:
    if row.provider == "invite":
        return Identity(
            provider="invite", provider_label="Invite link", label=row.label, icon="link"
        )
    label = _PROVIDER_LABELS.get(row.provider, row.provider.title())
    return Identity(provider=row.provider, provider_label=label, label=row.label)


def _shown_platform(platform: Platform | None) -> ShownPlatform:
    """The chat app a workspace lives in; a web or CLI one shows as the web app's own."""
    return platform if platform in CHAT_PLATFORMS else "web"


async def _workspaces(runtime: WebRuntime, caller: WebCaller) -> list[Workspace]:
    """Every workspace this sign-in reaches and the person is still in, in a few reads
    however many there are."""
    async with runtime.sessionmaker() as db:
        ids = await sessions_store.list_session_people(db, session_id=caller.session.id)
        everyone = await people_store.get_people(db, person_ids=ids)
        standings = await chat_standings(db, everyone)
        people = [p for p in everyone if not has_left(standings, p)]
        roles = await roles_of(db, people, standings)
        members = await people_store.count_by_tenant(db, tenant_ids={p.tenant_id for p in people})
        platforms = await tenants_store.list_platforms(db, {p.tenant_id for p in people})
    return [
        Workspace(
            id=str(p.tenant_id),
            name=p.workspace_name,
            platform=_shown_platform(platforms.get(p.tenant_id)),
            role=roles[p.id],
            members=members.get(p.tenant_id, 0),
        )
        for p in people
    ]


async def _invited_by(runtime: WebRuntime, person: WebPersonRow) -> str | None:
    """Who invited a visitor, by name; None for members and for an erased inviter."""
    if person.kind != "visitor" or person.invited_by_account_id is None:
        return None
    async with runtime.sessionmaker() as db:
        inviter = await people_store.get_person_for_account(
            db, tenant_id=person.tenant_id, account_id=person.invited_by_account_id
        )
    return None if inviter is None else inviter.display_name


async def _move_on(runtime: WebRuntime, caller: WebCaller) -> None:
    """Switch the session from a workspace the person left to the first one they are
    still in and may sign in to; 403 when there is none."""
    async with runtime.sessionmaker() as db, db.begin():
        ids = await sessions_store.list_session_people(db, session_id=caller.session.id)
        people = await people_store.get_people(db, person_ids=ids)
        standings = await chat_standings(db, people)
        for person in people:
            if (
                person.id != caller.person.id
                and person.status != "suspended"
                and not has_left(standings, person)
                and await signin_allowed_in(db, tenant_id=person.tenant_id)
            ):
                await sessions_store.switch_person(
                    db, session_id=caller.session.id, tenant_id=person.tenant_id
                )
                return
    raise HTTPException(status_code=403, detail=LEFT_WORKSPACE)


def me_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter()

    @router.get("/api/me")
    async def me(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        response: Response,
        caller: Annotated[WebCaller, Depends(caller_who_may_have_left)],
    ) -> Me:
        if caller.left:
            await _move_on(runtime, caller)
            caller = await current_caller(request, response)
        workspaces = await _workspaces(runtime, caller)
        current = next(w for w in workspaces if w.id == str(caller.person.tenant_id))
        return Me(
            id=str(caller.person.id),
            name=caller.person.display_name,
            email=caller.person.email or "",
            workspace=current,
            workspaces=workspaces,
            role=caller.role,
            modules=list(caller.modules),
            capabilities=sorted(caller.capabilities),
            identities=[identity_json(row) for row in caller.identities],
            credit=await credit_meter(runtime, caller),
            invited_by=await _invited_by(runtime, caller.person),
            dev=runtime.web.dev,
        )

    @router.post("/api/workspaces/switch")
    async def switch(  # pyright: ignore[reportUnusedFunction]
        body: SwitchBody, caller: Annotated[WebCaller, Depends(caller_who_may_be_suspended)]
    ) -> dict[str, bool]:
        try:
            tenant_id = uuid.UUID(body.id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Unknown workspace.") from None
        async with runtime.sessionmaker() as db, db.begin():
            # The recheck only looks at the workspace the session is on, so look at this one.
            switched = (
                await sessions_store.switch_person(
                    db, session_id=caller.session.id, tenant_id=tenant_id
                )
                if await signin_allowed_in(db, tenant_id=tenant_id)
                else None
            )
            if switched is not None:
                people = await people_store.get_people(db, person_ids=[switched.current_person_id])
                standings = await chat_standings(db, people)
                if any(has_left(standings, p) for p in people):
                    # Raised inside, so the switch rolls back.
                    raise HTTPException(status_code=403, detail=LEFT_WORKSPACE)
        if switched is None:
            raise HTTPException(status_code=404, detail="Unknown workspace.")
        return {"ok": True}

    return router
