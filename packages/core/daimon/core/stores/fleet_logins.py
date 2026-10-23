"""Reads that describe an agent's stored logins, without ever loading a value."""

from __future__ import annotations

import uuid

from daimon.core._models import AgentFile, AgentMcpCredential
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def mcp_credential_url(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, credential_id: uuid.UUID
) -> str | None:
    """The server URL a stored connector login belongs to (never its token)."""
    return await session.scalar(
        select(AgentMcpCredential.mcp_server_url).where(
            AgentMcpCredential.id == credential_id,
            AgentMcpCredential.tenant_id == tenant_id,
            AgentMcpCredential.agent_id == agent_id,
        )
    )


async def stored_key_setters(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, key: str
) -> tuple[uuid.UUID | None, uuid.UUID | None] | None:
    """Who created and who last set a stored key (never its content); None if absent."""
    row = (
        await session.execute(
            select(AgentFile.created_by_account_id, AgentFile.last_set_by_account_id).where(
                AgentFile.tenant_id == tenant_id,
                AgentFile.agent_id == agent_id,
                AgentFile.key == key,
            )
        )
    ).one_or_none()
    return (row.created_by_account_id, row.last_set_by_account_id) if row is not None else None
