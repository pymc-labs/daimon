"""Async store for agent_mcp_credentials.

UPSERT on (tenant_id, agent_id, mcp_server_url): re-entering a token for a
server the agent already has replaces the ciphertext and bumps updated_at.
No try/except — DB exceptions propagate to the caller's boundary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.core._models import AgentMcpCredential
from daimon.core.stores.domain import AgentMcpCredentialRow
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_credential(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    mcp_server_url: str,
    encrypted_token: bytes,
) -> AgentMcpCredentialRow:
    now = datetime.now(tz=UTC)
    stmt = (
        pg_insert(AgentMcpCredential)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            agent_id=agent_id,
            mcp_server_url=mcp_server_url,
            encrypted_token=encrypted_token,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_agent_mcp_credentials_tenant_agent_url",
            set_={"encrypted_token": encrypted_token, "updated_at": now},
        )
        .returning(AgentMcpCredential)
    )
    result = await session.execute(stmt)
    return AgentMcpCredentialRow.model_validate(result.scalar_one())


async def list_credentials(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> tuple[AgentMcpCredentialRow, ...]:
    """Every stored credential for this agent, oldest first."""
    result = await session.execute(
        select(AgentMcpCredential)
        .where(
            AgentMcpCredential.tenant_id == tenant_id,
            AgentMcpCredential.agent_id == agent_id,
        )
        .order_by(AgentMcpCredential.created_at)
    )
    return tuple(AgentMcpCredentialRow.model_validate(orm) for orm in result.scalars())


async def delete_credential(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    mcp_server_url: str,
) -> bool:
    """Delete the credential for one server. ``True`` when a row was removed."""
    result = await session.execute(
        select(AgentMcpCredential).where(
            AgentMcpCredential.tenant_id == tenant_id,
            AgentMcpCredential.agent_id == agent_id,
            AgentMcpCredential.mcp_server_url == mcp_server_url,
        )
    )
    orm = result.scalar_one_or_none()
    if orm is None:
        return False
    await session.delete(orm)
    return True
