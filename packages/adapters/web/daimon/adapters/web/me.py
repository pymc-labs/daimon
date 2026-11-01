"""`/api/me` and workspace switching: who the app is showing, and where."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from daimon.adapters.web.access import Capability, Role
from daimon.adapters.web.caller import (
    WebCaller,
    caller_who_may_be_suspended,
    current_caller,
    roles_of,
    signin_allowed_in,
)
from daimon.adapters.web.credits import CreditMeter, credit_meter
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.config import EmbeddedAppSettings, WebModuleKey
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores.domain import WebIdentityRow, WebPersonRow
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field


def _omit_none(value: object) -> bool:
    return value is None


class Workspace(BaseModel):
    id: str
    name: str
    platform: Literal["discord", "slack", "teams", "web"]
    role: Role
    members: int


class NavItem(BaseModel):
    label: str
    path: str
    icon: str


class EmbeddedApp(BaseModel):
    key: str
    name: str
    icon: str
    origin: str
    nav: list[NavItem]
    connections_path: str | None


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
    apps: list[EmbeddedApp]
    dev: bool


class SwitchBody(BaseModel):
    id: str


_PROVIDER_LABELS = {"discord": "Discord", "slack": "Slack", "microsoft": "Microsoft"}


def identity_json(runtime: WebRuntime, row: WebIdentityRow) -> Identity:
    oidc = runtime.web.oidc
    if row.provider == "oidc" and oidc is not None:
        return Identity(provider="oidc", provider_label=oidc.label, label=row.label, icon=oidc.icon)
    if row.provider == "invite":
        return Identity(
            provider="invite", provider_label="Invite link", label=row.label, icon="link"
        )
    label = _PROVIDER_LABELS.get(row.provider, row.provider.title())
    return Identity(provider=row.provider, provider_label=label, label=row.label)


def _app(app: EmbeddedAppSettings) -> EmbeddedApp:
    return EmbeddedApp(
        key=app.key,
        name=app.name,
        icon=app.icon,
        origin=app.origin,
        nav=[NavItem(label=n.label, path=n.path, icon=n.icon) for n in app.nav],
        connections_path=app.connections_path,
    )


def _workspace_name(runtime: WebRuntime, person: WebPersonRow) -> str:
    """The sign-in provider's configured name for its workspace, else the name stored with
    the person (what a chat platform called the workspace)."""
    oidc = runtime.web.oidc
    if oidc is not None and oidc.tenant_id == person.tenant_id:
        return oidc.workspace_name
    return person.workspace_name


async def _workspaces(runtime: WebRuntime, caller: WebCaller) -> list[Workspace]:
    """Every workspace this sign-in reaches, in a few reads however many there are."""
    async with runtime.sessionmaker() as db:
        ids = await sessions_store.list_session_people(db, session_id=caller.session.id)
        people = await people_store.get_people(db, person_ids=ids)
        identities = await identities_store.list_for_people(db, person_ids=[p.id for p in people])
        roles = await roles_of(db, people, identities)
        members = await people_store.count_by_tenant(db, tenant_ids={p.tenant_id for p in people})
    return [
        Workspace(
            id=str(p.tenant_id),
            name=_workspace_name(runtime, p),
            platform="web",
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


def me_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter()

    @router.get("/api/me")
    async def me(caller: Annotated[WebCaller, Depends(current_caller)]) -> Me:  # pyright: ignore[reportUnusedFunction]
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
            identities=[identity_json(runtime, row) for row in caller.identities],
            credit=await credit_meter(runtime, caller),
            invited_by=await _invited_by(runtime, caller.person),
            apps=[_app(app) for app in runtime.web.apps],
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
                if await signin_allowed_in(
                    db, provider=caller.session.provider, tenant_id=tenant_id
                )
                else None
            )
        if switched is None:
            raise HTTPException(status_code=404, detail="Unknown workspace.")
        return {"ok": True}

    return router
