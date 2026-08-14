"""Tests for the agent-scoped external MCP credential store."""

from __future__ import annotations

import uuid

from cryptography.fernet import Fernet
from daimon.core.agent_mcp_credentials import (
    resolve_agent_mcp_credentials,
    save_agent_mcp_credential,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.stores import agent_mcp_credentials as cred_store
from daimon.core.stores.domain import AgentMcpCredentialRow
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_upsert_credential_returns_pydantic_row(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    row = await cred_store.upsert_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url="https://example.com/mcp",
        encrypted_token=b"ciphertext",
    )

    assert isinstance(row, AgentMcpCredentialRow), "store should return Pydantic, not ORM"
    assert row.mcp_server_url == "https://example.com/mcp"
    assert row.encrypted_token == b"ciphertext"


async def test_upsert_credential_replaces_token_for_the_same_url(
    db_session: AsyncSession,
) -> None:
    """Re-entering a token for a server the agent already has must replace it,
    not accumulate rows MA would then race over."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    await cred_store.upsert_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url="https://example.com/mcp",
        encrypted_token=b"old",
    )
    await cred_store.upsert_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url="https://example.com/mcp",
        encrypted_token=b"new",
    )

    rows = await cred_store.list_credentials(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert len(rows) == 1, "one row per (tenant, agent, url)"
    assert rows[0].encrypted_token == b"new", "the newer token wins"


async def test_list_credentials_scopes_to_the_requested_agent(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    mine = uuid.uuid4()
    theirs = uuid.uuid4()

    await cred_store.upsert_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=mine,
        mcp_server_url="https://mine.example.com/mcp",
        encrypted_token=b"a",
    )
    await cred_store.upsert_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=theirs,
        mcp_server_url="https://theirs.example.com/mcp",
        encrypted_token=b"b",
    )

    rows = await cred_store.list_credentials(db_session, tenant_id=tenant.id, agent_id=mine)
    assert [r.mcp_server_url for r in rows] == ["https://mine.example.com/mcp"], (
        "another agent's credential must never leak into this agent's session"
    )


async def test_delete_credential_reports_whether_a_row_was_removed(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await cred_store.upsert_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url="https://example.com/mcp",
        encrypted_token=b"x",
    )

    assert (
        await cred_store.delete_credential(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            mcp_server_url="https://example.com/mcp",
        )
        is True
    ), "deleting an existing credential reports True"
    assert (
        await cred_store.delete_credential(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            mcp_server_url="https://example.com/mcp",
        )
        is False
    ), "deleting an absent credential is idempotent and reports False"


async def test_save_then_resolve_round_trips_the_plaintext_token(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The token is what MA needs; encryption must be transparent to callers."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    fernet = build_multifernet((Fernet.generate_key().decode(),))

    await save_agent_mcp_credential(
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url="https://internal.example.com/mcp",
        plaintext_token="tok_secret",
    )

    resolved = await resolve_agent_mcp_credentials(
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant.id,
        agent_id=agent_id,
    )

    assert len(resolved) == 1
    assert resolved[0].mcp_server_url == "https://internal.example.com/mcp"
    assert resolved[0].token == "tok_secret", "resolve must return the decrypted token"


async def test_resolve_returns_empty_for_an_agent_with_no_credentials(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Empty means 'no external MCP servers', never 'something broke'."""
    tenant = await make_tenant(db_session)
    fernet = build_multifernet((Fernet.generate_key().decode(),))

    resolved = await resolve_agent_mcp_credentials(
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
    )

    assert resolved == (), "no rows resolves to an empty tuple"


async def test_stored_token_is_not_recoverable_without_the_key(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The DB row must hold ciphertext — this table is the only place the token
    is readable, so a DB dump alone must not yield live MCP tokens."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    fernet = build_multifernet((Fernet.generate_key().decode(),))

    await save_agent_mcp_credential(
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url="https://internal.example.com/mcp",
        plaintext_token="tok_secret",
    )

    rows = await cred_store.list_credentials(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert b"tok_secret" not in rows[0].encrypted_token, "token must be stored encrypted"
