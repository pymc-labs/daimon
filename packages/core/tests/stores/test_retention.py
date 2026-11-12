"""Batched retention deletes against real Postgres: what goes and what stays."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core._models import CredentialRequest, McpOAuthFlow
from daimon.core.credential_requests import mint_request_token
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores import retention
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 6, 1, tzinfo=UTC)
OLD = NOW - timedelta(days=200)


async def _request(
    session: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID, *, flow: str | None
) -> str:
    request = await requests_store.create_credential_request(
        session,
        token=mint_request_token(),
        kind="mcp_oauth",
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        account_id=account_id,
        target="notes",
        mcp_server_url="https://mcp.example.com/mcp",
        requester_platform_user_id="requester-1",
        channel_id="chan-1",
        expires_at=OLD,
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_test",
        target_name="daimon",
        requested_work=None,
    )
    await session.execute(
        update(CredentialRequest)
        .where(CredentialRequest.token == request.token)
        .values(used_at=OLD)
    )
    if flow is not None:
        await flows_store.create_flow(
            session,
            state="st_" + uuid.uuid4().hex,
            request_token=request.token,
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=request.agent_id,
            server_name="notes",
            mcp_server_url="https://mcp.example.com/mcp",
            redirect_uri="https://d.example/oauth/mcp/callback",
            code_verifier="verifier",
            expires_at=OLD,
        )
        if flow == "completed":
            await session.execute(
                update(McpOAuthFlow)
                .where(McpOAuthFlow.request_token == request.token)
                .values(used_at=OLD, completed_at=OLD)
            )
    return request.token


async def test_credential_sweep_keeps_requests_behind_completed_sign_ins(
    db_session: AsyncSession,
) -> None:
    """A completed MCP sign-in cascades from its request, so the request must stay."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    signed_in = await _request(db_session, tenant.id, account.id, flow="completed")
    abandoned = await _request(db_session, tenant.id, account.id, flow="started")
    plain = await _request(db_session, tenant.id, account.id, flow=None)

    removed = await retention.delete_credential_requests(db_session, before=NOW, limit=100)

    tokens = set(await db_session.scalars(select(CredentialRequest.token)))
    assert removed == 2, "the abandoned flow's request and the plain request go"
    assert tokens == {signed_in}, "only the request behind a completed sign-in stays"
    flows = set(await db_session.scalars(select(McpOAuthFlow.request_token)))
    assert flows == {signed_in}, "the completed flow survives; the abandoned one cascades away"
    assert abandoned not in tokens and plain not in tokens, "used requests past the cutoff go"
