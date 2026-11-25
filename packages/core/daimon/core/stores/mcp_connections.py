"""Async store for a workspace's shared MCP connections and their tools.

A connection's ciphertext leaves this module only through `McpConnectionRow.encrypted_auth`,
which no page or log reads. No try/except: database errors reach the caller's boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from daimon.core._models import McpConnection, McpConnectionTool
from daimon.core.stores.domain import (
    DiscoveredTool,
    McpConnectionRow,
    McpConnectionStatus,
    McpConnectionToolRow,
)
from sqlalchemy import and_, case, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def list_connections(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[McpConnectionRow]:
    rows = await session.scalars(
        select(McpConnection)
        .where(McpConnection.tenant_id == tenant_id)
        .order_by(McpConnection.name)
    )
    return [McpConnectionRow.model_validate(row) for row in rows]


async def get_connection(
    session: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> McpConnectionRow | None:
    row = await session.scalar(
        select(McpConnection).where(
            McpConnection.tenant_id == tenant_id, McpConnection.id == connection_id
        )
    )
    return None if row is None else McpConnectionRow.model_validate(row)


async def get_connection_by_name(
    session: AsyncSession, *, tenant_id: uuid.UUID, name: str
) -> McpConnectionRow | None:
    row = await session.scalar(
        select(McpConnection).where(
            McpConnection.tenant_id == tenant_id, McpConnection.name == name
        )
    )
    return None if row is None else McpConnectionRow.model_validate(row)


async def create_connection(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    url: str,
    encrypted_auth: bytes | None,
    scope: str | None,
) -> McpConnectionRow:
    row = McpConnection(
        tenant_id=tenant_id, name=name, url=url, encrypted_auth=encrypted_auth, scope=scope
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return McpConnectionRow.model_validate(row)


async def update_connection(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    values: dict[str, object],
) -> bool:
    """Set columns of one connection; `values` names `McpConnection` columns. False: no row."""
    result = await session.execute(
        update(McpConnection)
        .where(McpConnection.tenant_id == tenant_id, McpConnection.id == connection_id)
        .values(**values)
    )
    return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownVariableType]


async def mark_connection(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    status: McpConnectionStatus,
    last_error: str | None,
) -> None:
    """Record how the last read of the upstream went; a pause stays."""
    await update_connection(
        session,
        tenant_id=tenant_id,
        connection_id=connection_id,
        values={
            "status": case((McpConnection.status == "paused", McpConnection.status), else_=status),
            "last_error": last_error,
        },
    )


async def record_refresh(
    session: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, note: str | None
) -> None:
    """A read of the upstream went through: an error clears, a pause stays, `note` is kept."""
    await update_connection(
        session,
        tenant_id=tenant_id,
        connection_id=connection_id,
        values={
            "status": case((McpConnection.status == "error", "active"), else_=McpConnection.status),
            "last_error": None,
            "note": note,
            "refreshed_at": func.now(),
        },
    )


async def delete_connection(
    session: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> bool:
    result = await session.execute(
        delete(McpConnection).where(
            McpConnection.tenant_id == tenant_id, McpConnection.id == connection_id
        )
    )
    return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownVariableType]


async def list_tools(
    session: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID | None = None
) -> list[McpConnectionToolRow]:
    stmt = select(McpConnectionTool).where(McpConnectionTool.tenant_id == tenant_id)
    if connection_id is not None:
        stmt = stmt.where(McpConnectionTool.connection_id == connection_id)
    rows = await session.scalars(stmt.order_by(McpConnectionTool.name))
    return [McpConnectionToolRow.model_validate(row) for row in rows]


async def list_served(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[tuple[McpConnectionRow, McpConnectionToolRow]]:
    """Every enabled tool of an active connection, by connection then tool name; the caller
    drops a changed one (`McpConnectionToolRow.changed`)."""
    result = await session.execute(
        select(McpConnection, McpConnectionTool)
        .join(
            McpConnectionTool,
            and_(
                McpConnectionTool.connection_id == McpConnection.id,
                McpConnectionTool.enabled.is_(True),
            ),
        )
        .where(McpConnection.tenant_id == tenant_id, McpConnection.status == "active")
        .order_by(McpConnection.name, McpConnectionTool.name)
    )
    return [
        (McpConnectionRow.model_validate(conn), McpConnectionToolRow.model_validate(tool))
        for conn, tool in result
    ]


async def sync_tools(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    discovered: Sequence[DiscoveredTool],
) -> None:
    """Store what the upstream lists. A new tool starts disabled and unapproved; a known
    tool keeps its approved text and only its `seen_*` moves; a tool the upstream dropped goes.
    `writes` only rises on a read, unless an admin set it."""
    names = [tool.name for tool in discovered]
    await session.execute(
        delete(McpConnectionTool).where(
            McpConnectionTool.connection_id == connection_id,
            McpConnectionTool.name.not_in(names),
        )
    )
    for tool in discovered:
        stmt = insert(McpConnectionTool).values(
            connection_id=connection_id,
            name=tool.name,
            tenant_id=tenant_id,
            seen_description=tool.description,
            seen_schema=tool.input_schema,
            writes=tool.writes,
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["connection_id", "name"],
                set_={
                    "seen_description": stmt.excluded.seen_description,
                    "seen_schema": stmt.excluded.seen_schema,
                    # An admin's own setting stays; a read never lowers it.
                    "writes": case(
                        (McpConnectionTool.writes_set.is_(True), McpConnectionTool.writes),
                        else_=McpConnectionTool.writes | stmt.excluded.writes,
                    ),
                    "updated_at": func.now(),
                },
            )
        )


async def update_tool(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    name: str,
    values: dict[str, object],
) -> bool:
    """Set columns of one tool; `values` names `McpConnectionTool` columns. False: no row."""
    result = await session.execute(
        update(McpConnectionTool)
        .where(
            McpConnectionTool.tenant_id == tenant_id,
            McpConnectionTool.connection_id == connection_id,
            McpConnectionTool.name == name,
        )
        .values(**values)
    )
    return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownVariableType]
