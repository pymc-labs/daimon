"""Async store for agent_mcp_credentials.

UPSERT on (tenant_id, agent_id, mcp_server_url): re-entering a token for a
server the agent already has replaces the ciphertext and bumps updated_at.
No try/except — DB exceptions propagate to the caller's boundary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.core._models import AgentMcpCredential
from daimon.core.mcp_server_url import same_mcp_url
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
    """Delete the credential for one server. ``True`` when a row was removed.

    Matches every row for the same server in `canonical_mcp_url` form: rows
    written by the setup panel kept the URL as typed.
    """
    result = await session.execute(
        select(AgentMcpCredential).where(
            AgentMcpCredential.tenant_id == tenant_id,
            AgentMcpCredential.agent_id == agent_id,
        )
    )
    rows = [orm for orm in result.scalars() if same_mcp_url(orm.mcp_server_url, mcp_server_url)]
    for orm in rows:
        await session.delete(orm)
    return bool(rows)


async def replace_token_by_id(
    session: AsyncSession, *, id: uuid.UUID, encrypted_token: bytes
) -> None:
    """Rotate one row's ciphertext in place, keeping its stored URL."""
    orm = await session.get(AgentMcpCredential, id)
    if orm is not None:
        orm.encrypted_token = encrypted_token
        orm.updated_at = datetime.now(tz=UTC)
        await session.flush()


async def delete_credential_by_id(session: AsyncSession, *, id: uuid.UUID) -> None:
    orm = await session.get(AgentMcpCredential, id)
    if orm is not None:
        await session.delete(orm)
        await session.flush()
