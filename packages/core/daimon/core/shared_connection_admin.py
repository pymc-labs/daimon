"""An admin's changes to a workspace's shared MCP connections, for the web page and chat alike.

Who may change them (a workspace admin or owner, with `shared_connections` on) is the caller's
check. Each change writes an admin audit row in its own transaction; no view holds the
credential. The rules for serving and calling live in `daimon.core.shared_connections`.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import datetime
from typing import Literal

from cryptography.fernet import MultiFernet
from daimon.core import shared_connections as shared
from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, Sessions, place_feature_or_none
from daimon.core.mcp_server_url import shown_mcp_url
from daimon.core.shared_connections import SharedConnectionError
from daimon.core.stores import mcp_connections as store
from daimon.core.stores.domain import McpConnectionRow, McpConnectionToolRow
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.web_admin import audit
from pydantic import BaseModel, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

NO_CONNECTION = "This connection isn't here."
READ_ONLY = "This connection comes from Daimon's configuration. Change it there."
OFF = "Shared connections are turned off here. Turn them on in Settings first."


class ConnectionTool(BaseModel):
    name: str
    description: str
    """What callers get: what was approved."""
    seen_description: str
    seen_schema: dict[str, JsonValue]
    """The whole schema a client is shown once the tool is on."""
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
    note: str | None
    refreshed_at: str | None
    scope: str | None
    declared: bool
    has_token: bool
    tools: list[ConnectionTool]


class Connections(BaseModel):
    enabled: bool
    connections: list[Connection]


def _tool_out(tool: McpConnectionToolRow) -> ConnectionTool:
    return ConnectionTool(
        name=tool.name,
        description=tool.description,
        seen_description=tool.seen_description,
        seen_schema=tool.seen_schema,
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
        url=shown_mcp_url(row.url),
        status=row.status,
        error=row.last_error,
        note=row.note,
        refreshed_at=row.refreshed_at.isoformat() if row.refreshed_at else None,
        scope=row.scope,
        declared=row.declared,
        has_token=row.encrypted_auth is not None,
        tools=[_tool_out(t) for t in tools],
    )


def _scope(scope: str | None) -> str | None:
    if scope is not None and shared.SCOPE_PATTERN.fullmatch(scope) is None:
        raise SharedConnectionError("That scope isn't valid.")
    return scope


async def is_on(
    sessions: Sessions, settings: Settings, tenant_id: uuid.UUID, platform: str
) -> bool:
    """Whether the workspace has the feature on; a failed read is off."""
    place = FeaturePlace(tenant_id, platform)
    found = await place_feature_or_none(sessions, settings, place, shared.FEATURE)
    return found is not None and found.on


async def listing(sessions: Sessions, tenant_id: uuid.UUID, *, on: bool) -> Connections:
    async with sessions() as db:
        rows = await store.list_connections(db, tenant_id=tenant_id)
        tools = await store.list_tools(db, tenant_id=tenant_id)
    return Connections(
        enabled=on,
        connections=[_out(r, [t for t in tools if t.connection_id == r.id]) for r in rows],
    )


async def _one(
    db: AsyncSession, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> McpConnectionRow:
    found = await store.get_connection(db, tenant_id=tenant_id, connection_id=connection_id)
    if found is None:
        raise SharedConnectionError(NO_CONNECTION, "missing")
    return found


async def view(sessions: Sessions, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> Connection:
    async with sessions() as db:
        row = await _one(db, tenant_id, connection_id)
        tools = await store.list_tools(db, tenant_id=tenant_id, connection_id=connection_id)
    return _out(row, tools)


async def refresh(
    sessions: Sessions,
    fernet: MultiFernet,
    policy: ToolSafetyPolicy,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> Connection:
    """Read the server's tools again; a failure is kept on the connection, not raised."""
    async with sessions() as db:
        row = await _one(db, tenant_id, connection_id)
    with contextlib.suppress(SharedConnectionError):
        await shared.refresh_connection(sessions, fernet, policy, row)
    return await view(sessions, tenant_id, connection_id)


async def add(
    sessions: Sessions,
    fernet: MultiFernet,
    policy: ToolSafetyPolicy,
    *,
    tenant_id: uuid.UUID,
    actor: uuid.UUID,
    name: str,
    url: str,
    token: str | None,
    scope: str | None,
    now: datetime,
) -> Connection:
    """Store a connection, then read its tools (all off until approved)."""
    async with sessions.begin() as db:
        row = await shared.add_connection(
            db, fernet, tenant_id=tenant_id, name=name, url=url, token=token, scope=_scope(scope)
        )
        await audit(
            db,
            tenant_id=tenant_id,
            actor_account_id=actor,
            action="Added shared connection",
            target=row.name,
            detail=shown_mcp_url(row.url),
            at=now,
        )
    return await refresh(sessions, fernet, policy, tenant_id=tenant_id, connection_id=row.id)


async def edit(
    sessions: Sessions,
    fernet: MultiFernet,
    *,
    tenant_id: uuid.UUID,
    actor: uuid.UUID,
    connection_id: uuid.UUID,
    status: Literal["active", "paused"] | None,
    scope: str | None,
    scope_set: bool,
    token: str | None,
    now: datetime,
) -> Connection:
    """Pause or resume, rescope or replace the token; a declared one takes only a pause."""
    values: dict[str, object] = {}
    async with sessions.begin() as db:
        row = await _one(db, tenant_id, connection_id)
        if row.declared and (scope_set or token is not None):
            raise SharedConnectionError(READ_ONLY, "read_only")
        if status is not None:
            values["status"] = status
            values["last_error"] = None
        if scope_set:
            values["scope"] = _scope(scope)
        if token is not None:
            values["encrypted_auth"] = shared.encrypt_auth(fernet, token)
        if values:
            await store.update_connection(
                db, tenant_id=tenant_id, connection_id=connection_id, values=values
            )
            await audit(
                db,
                tenant_id=tenant_id,
                actor_account_id=actor,
                action="Changed shared connection",
                target=row.name,
                detail=", ".join(sorted(k.replace("encrypted_auth", "token") for k in values)),
                at=now,
            )
    return await view(sessions, tenant_id, connection_id)


async def remove(
    sessions: Sessions,
    *,
    tenant_id: uuid.UUID,
    actor: uuid.UUID,
    connection_id: uuid.UUID,
    now: datetime,
) -> None:
    async with sessions.begin() as db:
        row = await _one(db, tenant_id, connection_id)
        if row.declared:
            raise SharedConnectionError(READ_ONLY, "read_only")
        await store.delete_connection(db, tenant_id=tenant_id, connection_id=connection_id)
        await audit(
            db,
            tenant_id=tenant_id,
            actor_account_id=actor,
            action="Removed shared connection",
            target=row.name,
            at=now,
        )


async def change_tool(
    sessions: Sessions,
    *,
    tenant_id: uuid.UUID,
    actor: uuid.UUID,
    connection_id: uuid.UUID,
    name: str,
    enabled: bool | None,
    seen: str | None,
    writes: bool | None,
    scope: str | None,
    scope_set: bool,
    now: datetime,
) -> Connection:
    """Turn a tool on as shown (`seen`) or off for the whole workspace, mark what it does,
    or rescope it."""
    async with sessions.begin() as db:
        row = await _one(db, tenant_id, connection_id)
        what: list[str] = []
        if enabled is True:
            if seen is None:
                raise SharedConnectionError("Review the tool before turning it on.", "conflict")
            await shared.approve_tool(
                db, tenant_id=tenant_id, connection_id=connection_id, name=name, seen=seen
            )
            what.append("approved")
        values: dict[str, object] = {}
        if enabled is False:
            values["enabled"] = False
            what.append("turned off")
        if writes is not None:
            # The admin's own switch is its approval.
            values.update(writes=writes, approved_writes=writes, writes_set=True)
            what.append("writes" if writes else "reads")
        if scope_set:
            values["scope"] = _scope(scope)
            what.append("scope")
        if values and not await store.update_tool(
            db, tenant_id=tenant_id, connection_id=connection_id, name=name, values=values
        ):
            raise SharedConnectionError(NO_CONNECTION, "missing")
        if what:
            await audit(
                db,
                tenant_id=tenant_id,
                actor_account_id=actor,
                action="Changed shared connection tool",
                target=f"{row.name}_{name}",
                detail=", ".join(what),
                at=now,
            )
    return await view(sessions, tenant_id, connection_id)
