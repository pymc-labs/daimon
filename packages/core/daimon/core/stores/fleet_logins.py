"""Reads that describe an agent's stored logins, without ever loading a value."""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import AgentFile, AgentMcpCredential
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def mcp_credential_url(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, credential_id: uuid.UUID
) -> tuple[str, datetime] | None:
    """The server URL a stored connector login belongs to and when it was last set.

    Never its token. None when there is no such login for this agent.
    """
    row = (
        await session.execute(
            select(AgentMcpCredential.mcp_server_url, AgentMcpCredential.updated_at).where(
                AgentMcpCredential.id == credential_id,
                AgentMcpCredential.tenant_id == tenant_id,
                AgentMcpCredential.agent_id == agent_id,
            )
        )
    ).one_or_none()
    return (row.mcp_server_url, row.updated_at) if row is not None else None


async def stored_key_setters(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, key: str
) -> tuple[uuid.UUID | None, uuid.UUID | None, datetime] | None:
    """Who created and who last set a stored key, and when (never its content)."""
    row = (
        await session.execute(
            select(
                AgentFile.created_by_account_id,
                AgentFile.last_set_by_account_id,
                AgentFile.updated_at,
            ).where(
                AgentFile.tenant_id == tenant_id,
                AgentFile.agent_id == agent_id,
                AgentFile.key == key,
            )
        )
    ).one_or_none()
    return (
        (row.created_by_account_id, row.last_set_by_account_id, row.updated_at)
        if row is not None
        else None
    )
