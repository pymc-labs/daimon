"""`/api/integrations/connections`: a workspace's shared MCP connections, for admins.

An admin adds one MCP server and its bearer token, reads the tools it offers, and turns each on
by approving the exact description and schema shown. No answer holds the credential. The rules
and the calls themselves live in `daimon.core.shared_connections`.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Annotated, Literal

from daimon.adapters.web.audit import audit
from daimon.adapters.web.caller import WebCaller, requires
from daimon.adapters.web.integrations import _shown_url  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import integration_views as views
from daimon.core import shared_connections as shared
from daimon.core.features.snapshot import FeaturePlace, place_feature_or_none
from daimon.core.integration_views import IntegrationsRefused
from daimon.core.stores import mcp_connections as store
from daimon.core.stores.domain import McpConnectionRow, McpConnectionToolRow
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

Viewer = Annotated[WebCaller, Depends(requires("integrations.view"))]
Manager = Annotated[WebCaller, Depends(requires("integrations.manage"))]

NO_CONNECTION = "This connection isn't here."
READ_ONLY = "This connection comes from the workspace's configuration. Change it there."
OFF = "Shared connections are turned off for this workspace. Turn them on in Settings first."
NO_KEY = "Shared connections aren't set up on this install."


class ConnectionTool(BaseModel):
    name: str
    description: str
    """What callers get: what was approved."""
    seen_description: str
    seen_arguments: list[str]
    seen: str
    """Send this back to approve exactly what is shown."""
    changed: bool
    enabled: bool
    writes: bool
    scope: str | None


class Connection(BaseModel):
    id: str
    name: str
    url: str
    status: Literal["active", "paused", "error"]
    error: str | None
    refreshed_at: str | None
    scope: str | None
    declared: bool
    has_token: bool
    tools: list[ConnectionTool]


class Connections(BaseModel):
    enabled: bool
    connections: list[Connection]


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


def _arguments(schema: dict[str, JsonValue]) -> list[str]:
    props = schema.get("properties")
    return sorted(str(k) for k in props) if isinstance(props, dict) else []


def _tool_out(tool: McpConnectionToolRow) -> ConnectionTool:
    return ConnectionTool(
        name=tool.name,
        description=tool.description,
        seen_description=tool.seen_description,
        seen_arguments=_arguments(tool.seen_schema),
        seen=shared.seen_digest(tool),
        changed=tool.enabled and tool.changed,
        enabled=tool.enabled,
        writes=tool.writes,
        scope=tool.scope,
    )


def _out(row: McpConnectionRow, tools: list[McpConnectionToolRow]) -> Connection:
    return Connection(
        id=str(row.id),
        name=row.name,
        url=_shown_url(row.url),
        status=row.status,
        error=row.last_error,
        refreshed_at=row.refreshed_at.isoformat() if row.refreshed_at else None,
        scope=row.scope,
        declared=row.declared,
        has_token=row.encrypted_auth is not None,
        tools=[_tool_out(t) for t in tools],
    )


def connections_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/integrations/connections")

    async def admin(db: AsyncSession, caller: WebCaller) -> None:
        try:
            views.require_admin((await caller.principal(db)).subject)
        except IntegrationsRefused as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    async def is_on(db: AsyncSession, caller: WebCaller) -> bool:
        place = FeaturePlace(caller.person.tenant_id, (await caller.principal(db)).platform)
        found = await place_feature_or_none(
            runtime.sessionmaker, runtime.settings, place, shared.FEATURE
        )
        return found is not None and found.on

    async def one(
        db: AsyncSession, caller: WebCaller, connection_id: uuid.UUID
    ) -> McpConnectionRow:
        found = await store.get_connection(
            db, tenant_id=caller.person.tenant_id, connection_id=connection_id
        )
        if found is None:
            raise HTTPException(status_code=404, detail=NO_CONNECTION)
        return found

    async def view(caller: WebCaller, connection_id: uuid.UUID) -> Connection:
        async with runtime.sessionmaker() as db:
            row = await one(db, caller, connection_id)
            tools = await store.list_tools(
                db, tenant_id=caller.person.tenant_id, connection_id=connection_id
            )
        return _out(row, tools)

    async def editable(
        db: AsyncSession, caller: WebCaller, connection_id: uuid.UUID
    ) -> McpConnectionRow:
        await admin(db, caller)
        if not await is_on(db, caller):
            raise HTTPException(status_code=409, detail=OFF)
        row = await one(db, caller, connection_id)
        if row.declared:
            raise HTTPException(status_code=403, detail=READ_ONLY)
        return row

    async def refresh(row: McpConnectionRow) -> None:
        # A failure is kept on the connection as its status and error.
        with contextlib.suppress(shared.SharedConnectionError):
            await shared.refresh_connection(
                runtime.sessionmaker, runtime.fernet, runtime.settings.tool_safety, row
            )

    @router.get("")
    async def listing(caller: Viewer) -> Connections:  # pyright: ignore[reportUnusedFunction]
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            await admin(db, caller)
            rows = await store.list_connections(db, tenant_id=tenant_id)
            tools = await store.list_tools(db, tenant_id=tenant_id)
            on = await is_on(db, caller)
        return Connections(
            enabled=on,
            connections=[_out(r, [t for t in tools if t.connection_id == r.id]) for r in rows],
        )

    @router.post("", status_code=201)
    async def add(body: AddBody, caller: Manager) -> Connection:  # pyright: ignore[reportUnusedFunction]
        tenant_id, now = caller.person.tenant_id, runtime.now()
        async with runtime.sessionmaker() as db:
            await admin(db, caller)
            if not await is_on(db, caller):
                raise HTTPException(status_code=409, detail=OFF)
        try:
            async with runtime.sessionmaker.begin() as db:
                row = await shared.add_connection(
                    db,
                    runtime.fernet,
                    tenant_id=tenant_id,
                    name=body.name,
                    url=body.url,
                    token=body.token,
                    scope=body.scope,
                )
                await audit(
                    db,
                    tenant_id=tenant_id,
                    actor_account_id=caller.person.account_id,
                    action="Added shared connection",
                    target=row.name,
                    detail=_shown_url(row.url),
                    at=now,
                )
        except shared.SharedConnectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await refresh(row)
        return await view(caller, row.id)

    @router.post("/{connection_id}/refresh")
    async def read_again(connection_id: uuid.UUID, caller: Manager) -> Connection:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker() as db:
            row = await editable(db, caller, connection_id)
        await refresh(row)
        return await view(caller, connection_id)

    @router.patch("/{connection_id}")
    async def edit(  # pyright: ignore[reportUnusedFunction]
        connection_id: uuid.UUID, body: EditBody, caller: Manager
    ) -> Connection:
        tenant_id, now = caller.person.tenant_id, runtime.now()
        values: dict[str, object] = {}
        async with runtime.sessionmaker.begin() as db:
            row = await editable(db, caller, connection_id)
            if body.status is not None:
                values["status"] = body.status
                values["last_error"] = None
            if "scope" in body.model_fields_set:
                values["scope"] = body.scope
            if body.token is not None:
                try:
                    values["encrypted_auth"] = shared.encrypt_auth(runtime.fernet, body.token)
                except shared.SharedConnectionError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
            if values:
                await store.update_connection(
                    db, tenant_id=tenant_id, connection_id=connection_id, values=values
                )
                await audit(
                    db,
                    tenant_id=tenant_id,
                    actor_account_id=caller.person.account_id,
                    action="Changed shared connection",
                    target=row.name,
                    detail=", ".join(sorted(k.replace("encrypted_auth", "token") for k in values)),
                    at=now,
                )
        return await view(caller, connection_id)

    @router.delete("/{connection_id}", status_code=204)
    async def remove(connection_id: uuid.UUID, caller: Manager) -> Response:  # pyright: ignore[reportUnusedFunction]
        tenant_id, now = caller.person.tenant_id, runtime.now()
        async with runtime.sessionmaker.begin() as db:
            await admin(db, caller)
            row = await one(db, caller, connection_id)
            if row.declared:
                raise HTTPException(status_code=403, detail=READ_ONLY)
            await store.delete_connection(db, tenant_id=tenant_id, connection_id=connection_id)
            await audit(
                db,
                tenant_id=tenant_id,
                actor_account_id=caller.person.account_id,
                action="Removed shared connection",
                target=row.name,
                at=now,
            )
        return Response(status_code=204)

    @router.patch("/{connection_id}/tools/{name}")
    async def tool(  # pyright: ignore[reportUnusedFunction]
        connection_id: uuid.UUID, name: str, body: ToolBody, caller: Manager
    ) -> Connection:
        tenant_id, now = caller.person.tenant_id, runtime.now()
        async with runtime.sessionmaker.begin() as db:
            row = await editable(db, caller, connection_id)
            what: list[str] = []
            try:
                if body.enabled is True:
                    if body.seen is None:
                        raise shared.SharedConnectionError("Review the tool before turning it on.")
                    await shared.approve_tool(
                        db,
                        tenant_id=tenant_id,
                        connection_id=connection_id,
                        name=name,
                        seen=body.seen,
                    )
                    what.append("approved")
            except shared.SharedConnectionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            values: dict[str, object] = {}
            if body.enabled is False:
                values["enabled"] = False
                what.append("turned off")
            if body.writes is not None:
                values.update(writes=body.writes, writes_set=True)
                what.append("writes" if body.writes else "reads")
            if "scope" in body.model_fields_set:
                values["scope"] = body.scope
                what.append("scope")
            if values and not await store.update_tool(
                db, tenant_id=tenant_id, connection_id=connection_id, name=name, values=values
            ):
                raise HTTPException(status_code=404, detail=NO_CONNECTION)
            if what:
                await audit(
                    db,
                    tenant_id=tenant_id,
                    actor_account_id=caller.person.account_id,
                    action="Changed shared connection tool",
                    target=f"{row.name}_{name}",
                    detail=", ".join(what),
                    at=now,
                )
        return await view(caller, connection_id)

    return router
