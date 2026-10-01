"""The DB read behind per-caller MCP server visibility — real Postgres.

`resolve_hidden_mcp_server_names` is the OAuth counterpart of the credential
mirror: it answers which of an agent's servers this caller has no way to
authenticate, so `create_session` can leave them off. The classification
itself is unit-tested in `test_mcp_personal_servers.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from daimon.core.agent_mcp_credentials import (
    resolve_hidden_mcp_server_names,
    save_agent_mcp_credential,
)
from daimon.core.credential_requests import mint_request_token
from daimon.core.github_credentials import build_multifernet
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
_SERVER_URL = "https://mcp.example.com/docs"


async def _record_sign_in(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    requester: str,
) -> None:
    """Drive one OAuth handshake through to a stored grant."""
    request = await requests_store.create_credential_request(
        session,
        token=mint_request_token(),
        kind="mcp_oauth",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=account_id,
        target="docs",
        mcp_server_url=_SERVER_URL,
        requester_platform_user_id=requester,
        channel_id="chan-1",
        expires_at=_NOW + timedelta(minutes=30),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_test",
        target_name="daimon",
        requested_work=None,
    )
    flow = await flows_store.create_flow(
        session,
        state="st_" + uuid.uuid4().hex,
        request_token=request.token,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=agent_id,
        server_name="docs",
        mcp_server_url=_SERVER_URL,
        redirect_uri="https://d.example/oauth/mcp/callback",
        code_verifier="verifier",
        expires_at=_NOW + timedelta(minutes=10),
    )
    await flows_store.consume_flow(session, state=flow.state, now=_NOW)
    await flows_store.mark_flow_completed(session, state=flow.state, now=_NOW)
    await session.commit()


async def test_a_server_one_person_connected_is_hidden_from_everyone_else(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """One account signs in and every other member's turn opens a server they
    hold no token for, so every reply carries the degraded-turn notice."""
    tenant = await make_tenant(db_session)
    connected = await make_account(db_session, tenant=tenant)
    bystander = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    await _record_sign_in(
        db_session,
        tenant_id=tenant.id,
        account_id=connected.id,
        agent_id=agent_id,
        requester="requester-connected",
    )

    assert await resolve_hidden_mcp_server_names(
        db_session_factory,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=bystander.id,
        server_urls={"docs": _SERVER_URL},
    ) == frozenset({"docs"}), "a member who never signed in must not be handed the server"
    assert (
        await resolve_hidden_mcp_server_names(
            db_session_factory,
            tenant_id=tenant.id,
            agent_id=agent_id,
            account_id=connected.id,
            server_urls={"docs": _SERVER_URL},
        )
        == frozenset()
    ), "the member who signed in keeps it"


async def test_nothing_is_hidden_for_an_agent_nobody_signed_in_to(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    assert (
        await resolve_hidden_mcp_server_names(
            db_session_factory,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            account_id=account.id,
            server_urls={"daimon-mcp": "https://daimon.example/mcp"},
        )
        == frozenset()
    ), "the common case must cost one read and hide nothing"


async def test_an_agent_wide_token_keeps_the_server_visible_to_everyone(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An admin's stored token is mirrored into every caller's vault, so the
    server stays even though somebody also signed in to it personally."""
    tenant = await make_tenant(db_session)
    connected = await make_account(db_session, tenant=tenant)
    bystander = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    await _record_sign_in(
        db_session,
        tenant_id=tenant.id,
        account_id=connected.id,
        agent_id=agent_id,
        requester="requester-connected",
    )
    await save_agent_mcp_credential(
        sessionmaker=db_session_factory,
        fernet=build_multifernet((Fernet.generate_key().decode(),)),
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url=_SERVER_URL,
        plaintext_token="tok_shared",
    )

    assert (
        await resolve_hidden_mcp_server_names(
            db_session_factory,
            tenant_id=tenant.id,
            agent_id=agent_id,
            account_id=bystander.id,
            server_urls={"docs": _SERVER_URL},
        )
        == frozenset()
    ), "a token every caller's vault gets is not a personal connection"


async def test_a_fork_of_the_agent_hides_the_copied_server_from_the_person_who_signed_in(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Forks copy the source's MCP servers raw; the grant stays in the vault of
    (person, source agent). On the fork nobody can open the server — the
    connector included — so it is hidden there until someone signs in on the
    fork, instead of failing every turn."""
    tenant = await make_tenant(db_session)
    connected = await make_account(db_session, tenant=tenant)
    source_agent_id = uuid.uuid4()
    fork_agent_id = uuid.uuid4()
    await _record_sign_in(
        db_session,
        tenant_id=tenant.id,
        account_id=connected.id,
        agent_id=source_agent_id,
        requester="requester-connected",
    )

    assert (
        await resolve_hidden_mcp_server_names(
            db_session_factory,
            tenant_id=tenant.id,
            agent_id=source_agent_id,
            account_id=connected.id,
            server_urls={"docs": _SERVER_URL},
        )
        == frozenset()
    ), "on the agent they signed in to, the connector keeps the server"
    assert await resolve_hidden_mcp_server_names(
        db_session_factory,
        tenant_id=tenant.id,
        agent_id=fork_agent_id,
        account_id=connected.id,
        server_urls={"docs": _SERVER_URL},
    ) == frozenset({"docs"}), "on a fork, the same person holds no grant and the server is hidden"
