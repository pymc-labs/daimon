"""`/api/profile/data/*`: the signed-in person's accounts in the workspace's data sources.

What they are known as in each source, and the sources they signed in to themselves (through
the provider's own OAuth page or a device code). Never a token: only who they signed in as.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

import httpx
from daimon.adapters.web.data_common import Viewer, refused, schemas_of, store
from daimon.adapters.web.runtime import WebRuntime
from daimon.data import identity, manage
from daimon.data.connect import (
    PROVIDERS,
    Connector,
    Failed,
    NotConfigured,
    configured_clients,
)
from daimon.data.identity import IdentityError
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

PROFILE: str = "/profile"
UNREACHABLE = "The sign-in page isn't reachable. Try again later."
NOT_SET_UP = "Signing in to that source isn't set up on this server."


class AccountOut(BaseModel):
    provider: str
    workspace: str
    name: str
    email: str | None
    proof: str | None
    linked_at: datetime | None


class ConnectionOut(BaseModel):
    source_id: uuid.UUID
    source: str
    provider: str
    subject: str
    status: str
    last_ok_at: datetime | None


class ConnectableOut(BaseModel):
    source_id: uuid.UUID
    name: str
    provider: str


class NoticeOut(BaseModel):
    kind: Literal["linked", "unlinked"]
    identity: str
    provider: str
    name: str


class Connected(BaseModel):
    accounts: list[AccountOut]
    notices: list[NoticeOut]
    """Links an admin changed since the last view; seeing them marks them read."""
    connections: list[ConnectionOut]
    connectable: list[ConnectableOut]


class Start(BaseModel):
    source_id: uuid.UUID


class Started(BaseModel):
    flow: Literal["code", "device"]
    url: str
    pending_id: uuid.UUID
    user_code: str
    interval: int
    source: str
    expires_at: datetime | None


class Polled(BaseModel):
    connected: bool


def profile_data_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/profile/data")
    schemas = schemas_of(runtime)
    settings = runtime.settings
    redirect = f"{str(runtime.web.public_url).rstrip('/')}/api/profile/data/connect/callback"
    keys = tuple(k.get_secret_value() for k in settings.crypto.keys)
    connector = Connector(
        schemas, keys, runtime.http_client, redirect, clients=configured_clients(settings.data)
    )

    @router.get("")
    async def connected(caller: Viewer) -> Connected:  # pyright: ignore[reportUnusedFunction]
        p = caller.person
        async with store(runtime) as conn:
            accounts = await manage.identities(conn, schemas, p.tenant_id, account_id=p.account_id)
            made = await manage.person_connections(conn, schemas, p.tenant_id, p.account_id)
            open_ = await manage.connectable(conn, schemas, p.tenant_id, list(PROVIDERS))
            notices = await identity.take_notices(conn, schemas, p.tenant_id, p.account_id)
        return Connected(
            accounts=[AccountOut.model_validate(a, from_attributes=True) for a in accounts],
            notices=[NoticeOut.model_validate(n, from_attributes=True) for n in notices],
            connections=[ConnectionOut.model_validate(c, from_attributes=True) for c in made],
            connectable=[ConnectableOut(source_id=i, name=n, provider=pr) for i, n, pr in open_],
        )

    @router.post("/connect")
    async def start(caller: Viewer, body: Start) -> Started:  # pyright: ignore[reportUnusedFunction]
        p = caller.person
        async with store(runtime) as conn:
            try:
                begun = await connector.start(
                    conn, tenant_id=p.tenant_id, account_id=p.account_id, source_id=body.source_id
                )
            except IdentityError as e:
                raise refused(e) from None
            except NotConfigured:
                raise HTTPException(status_code=409, detail=NOT_SET_UP) from None
            except (Failed, httpx.HTTPError):
                raise HTTPException(status_code=502, detail=UNREACHABLE) from None
        return Started(
            flow=begun.flow, url=begun.url, pending_id=begun.pending_id,
            user_code=begun.user_code, interval=begun.interval, source=begun.source,
            expires_at=begun.expires_at,
        )  # fmt: skip

    @router.get("/connect/callback")
    async def callback(  # pyright: ignore[reportUnusedFunction]
        caller: Viewer,
        state: Annotated[str, Query(max_length=500)],
        code: Annotated[str, Query(max_length=2000)] = "",
    ) -> RedirectResponse:
        """The provider sent the person back; whatever happens they land on their profile."""
        async with store(runtime) as conn:
            try:
                await connector.finish_code(
                    conn, account_id=caller.person.account_id, state=state, code=code
                )
            except (IdentityError, NotConfigured, Failed, httpx.HTTPError):
                return RedirectResponse(f"{PROFILE}?connect=failed", status_code=303)
        return RedirectResponse(PROFILE, status_code=303)

    @router.post("/connect/{pending_id}/poll")
    async def poll(caller: Viewer, pending_id: uuid.UUID) -> Polled:  # pyright: ignore[reportUnusedFunction]
        async with store(runtime) as conn:
            try:
                done = await connector.poll(
                    conn, account_id=caller.person.account_id, pending_id=pending_id
                )
            except IdentityError as e:
                raise refused(e) from None
            except (Failed, NotConfigured, httpx.HTTPError):
                raise HTTPException(status_code=502, detail=UNREACHABLE) from None
        return Polled(connected=done is not None)

    @router.delete("/connections/{source_id}", status_code=204)
    async def disconnect(caller: Viewer, source_id: uuid.UUID) -> None:  # pyright: ignore[reportUnusedFunction]
        async with store(runtime) as conn:
            try:
                await connector.disconnect(
                    conn, account_id=caller.person.account_id, source_id=source_id
                )
            except IdentityError as e:
                raise refused(e) from None

    return router
