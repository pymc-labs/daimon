"""`/api/integrations/connections`: a workspace's shared MCP connections, for admins.

An admin adds one MCP server and its bearer token, reads the tools it offers, and turns each on
by approving the exact description, schema and effect shown. A declared connection's address,
token and scope come from the deployment's file; its tools are approved here like any other.
No answer holds the credential. The changes live in `daimon.core.shared_connection_admin`,
shared with the chat tools.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from typing import Annotated, Literal

from daimon.adapters.web.caller import WebCaller, requires
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import integration_views as views
from daimon.core import shared_connection_admin as admin_ops
from daimon.core import shared_connections as shared
from daimon.core.integration_views import IntegrationsRefused
from daimon.core.shared_connection_admin import OFF, Connection, Connections
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

Viewer = Annotated[WebCaller, Depends(requires("integrations.view"))]
Manager = Annotated[WebCaller, Depends(requires("integrations.manage"))]

_CODES: dict[shared.ErrorKind, int] = {
    "invalid": 422,
    "conflict": 409,
    "read_only": 403,
    "missing": 404,
}


class AddBody(BaseModel):
    name: str = Field(max_length=32)
    url: str = Field(max_length=2048)
    token: str | None = Field(default=None, max_length=4096)
    scope: str | None = Field(default=None, pattern=shared.SCOPE_PATTERN.pattern)


class EditBody(BaseModel):
    status: Literal["active", "paused"] | None = None
    scope: str | None = Field(default=None, pattern=shared.SCOPE_PATTERN.pattern)
    token: str | None = Field(default=None, max_length=4096)


class ToolBody(BaseModel):
    enabled: bool | None = None
    seen: str | None = None
    writes: bool | None = None
    scope: str | None = Field(default=None, pattern=shared.SCOPE_PATTERN.pattern)


async def _answer[T](change: Awaitable[T]) -> T:
    try:
        return await change
    except shared.SharedConnectionError as exc:
        raise HTTPException(status_code=_CODES[exc.kind], detail=str(exc)) from exc


def connections_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/integrations/connections")
    sessions = runtime.sessionmaker

    async def admin(caller: WebCaller, *, on: bool = True) -> bool:
        """Refuse anyone but an admin or owner, and a change while the feature is off."""
        async with sessions() as db:
            principal = await caller.principal(db)
        try:
            views.require_admin(principal.subject)
        except IntegrationsRefused as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        found = await admin_ops.is_on(
            sessions, runtime.settings, caller.person.tenant_id, principal.platform
        )
        if on and not found:
            raise HTTPException(status_code=409, detail=OFF)
        return found

    @router.get("")
    async def listing(caller: Viewer) -> Connections:  # pyright: ignore[reportUnusedFunction]
        on = await admin(caller, on=False)
        return await admin_ops.listing(sessions, caller.person.tenant_id, on=on)

    @router.post("", status_code=201)
    async def add(body: AddBody, caller: Manager) -> Connection:  # pyright: ignore[reportUnusedFunction]
        await admin(caller)
        return await _answer(
            admin_ops.add(
                sessions,
                runtime.fernet,
                runtime.settings.tool_safety,
                tenant_id=caller.person.tenant_id,
                actor=caller.person.account_id,
                name=body.name,
                url=body.url,
                token=body.token,
                scope=body.scope,
                now=runtime.now(),
            )
        )

    @router.post("/{connection_id}/refresh")
    async def read_again(connection_id: uuid.UUID, caller: Manager) -> Connection:  # pyright: ignore[reportUnusedFunction]
        await admin(caller)
        return await _answer(
            admin_ops.refresh(
                sessions,
                runtime.fernet,
                runtime.settings.tool_safety,
                tenant_id=caller.person.tenant_id,
                connection_id=connection_id,
            )
        )

    @router.patch("/{connection_id}")
    async def edit(  # pyright: ignore[reportUnusedFunction]
        connection_id: uuid.UUID, body: EditBody, caller: Manager
    ) -> Connection:
        await admin(caller)
        return await _answer(
            admin_ops.edit(
                sessions,
                runtime.fernet,
                tenant_id=caller.person.tenant_id,
                actor=caller.person.account_id,
                connection_id=connection_id,
                status=body.status,
                scope=body.scope,
                scope_set="scope" in body.model_fields_set,
                token=body.token,
                now=runtime.now(),
            )
        )

    @router.delete("/{connection_id}", status_code=204)
    async def remove(connection_id: uuid.UUID, caller: Manager) -> Response:  # pyright: ignore[reportUnusedFunction]
        await admin(caller, on=False)
        await _answer(
            admin_ops.remove(
                sessions,
                tenant_id=caller.person.tenant_id,
                actor=caller.person.account_id,
                connection_id=connection_id,
                now=runtime.now(),
            )
        )
        return Response(status_code=204)

    @router.patch("/{connection_id}/tools/{name}")
    async def tool(  # pyright: ignore[reportUnusedFunction]
        connection_id: uuid.UUID, name: str, body: ToolBody, caller: Manager
    ) -> Connection:
        await admin(caller)
        return await _answer(
            admin_ops.change_tool(
                sessions,
                tenant_id=caller.person.tenant_id,
                actor=caller.person.account_id,
                connection_id=connection_id,
                name=name,
                enabled=body.enabled,
                seen=body.seen,
                writes=body.writes,
                scope=body.scope,
                scope_set="scope" in body.model_fields_set,
                now=runtime.now(),
            )
        )

    return router
