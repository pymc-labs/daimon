"""Integration tests for the mcp_oauth_flows store — real Postgres.

Covers the single-use consume (the callback's replay gate), the client
fill-in `/oauth/mcp/start` performs, and the cascade from the request row
that keeps the platform-user erasure complete without a second helper.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core.credential_requests import mint_request_token
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as store
from daimon.core.stores.domain import McpOAuthFlowRow
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


async def _seed(session: AsyncSession, *, expires_at: datetime | None = None) -> McpOAuthFlowRow:
    tenant = await make_tenant(session)
    account = await make_account(session, tenant=tenant)
    request = await requests_store.create_credential_request(
        session,
        token=mint_request_token(),
        kind="mcp_oauth",
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        account_id=account.id,
        target="notion",
        mcp_server_url="https://mcp.notion.com/mcp",
        requester_platform_user_id="requester-1",
        channel_id="chan-1",
        expires_at=_NOW + timedelta(minutes=30),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_test",
        target_name="daimon",
        requested_work=None,
    )
    return await store.create_flow(
        session,
        state="st_" + uuid.uuid4().hex,
        request_token=request.token,
        tenant_id=tenant.id,
        account_id=account.id,
        agent_id=request.agent_id,
        server_name="notion",
        mcp_server_url="https://mcp.notion.com/mcp",
        redirect_uri="https://d.example/oauth/mcp/callback",
        code_verifier="verifier",
        expires_at=expires_at or (_NOW + timedelta(minutes=10)),
    )


async def test_create_flow_round_trips_as_pydantic(db_session: AsyncSession) -> None:
    flow = await _seed(db_session)
    fetched = await store.get_flow(db_session, state=flow.state)
    assert isinstance(fetched, McpOAuthFlowRow), "store returns Pydantic, not ORM"
    assert fetched == flow, "get_flow returns exactly what create_flow wrote"
    assert fetched.client_id is None, "the client is unknown until /oauth/mcp/start runs"


async def test_save_flow_client_records_the_registered_client(db_session: AsyncSession) -> None:
    flow = await _seed(db_session)
    updated = await store.save_flow_client(
        db_session,
        state=flow.state,
        client_id="cid",
        client_secret_encrypted=None,
        token_endpoint_auth_method="none",
        token_endpoint="https://mcp.notion.com/token",
        authorization_endpoint="https://mcp.notion.com/authorize",
        resource="https://mcp.notion.com",
        scope="default",
    )
    assert updated is not None and updated.client_id == "cid"
    assert updated.token_endpoint == "https://mcp.notion.com/token"
    assert updated.authorization_endpoint == "https://mcp.notion.com/authorize"


async def test_save_flow_client_keeps_the_first_registration(db_session: AsyncSession) -> None:
    flow = await _seed(db_session)

    async def save(client_id: str) -> McpOAuthFlowRow | None:
        return await store.save_flow_client(
            db_session,
            state=flow.state,
            client_id=client_id,
            client_secret_encrypted=None,
            token_endpoint_auth_method="none",
            token_endpoint="https://mcp.notion.com/token",
            authorization_endpoint="https://mcp.notion.com/authorize",
            resource=None,
            scope=None,
        )

    first = await save("first")
    second = await save("second")
    assert first is not None and first.client_id == "first"
    assert second is None, "a second open of the link must not replace the registered client"
    fetched = await store.get_flow(db_session, state=flow.state)
    assert fetched is not None and fetched.client_id == "first"


async def test_consume_flow_is_single_use(db_session: AsyncSession) -> None:
    flow = await _seed(db_session)
    first = await store.consume_flow(db_session, state=flow.state, now=_NOW)
    second = await store.consume_flow(db_session, state=flow.state, now=_NOW)
    assert first is not None and first.used_at == _NOW, "the first callback spends the row"
    assert second is None, "a replayed callback finds nothing to spend"


async def test_consume_flow_refuses_an_expired_state(db_session: AsyncSession) -> None:
    flow = await _seed(db_session, expires_at=_NOW - timedelta(seconds=1))
    assert await store.consume_flow(db_session, state=flow.state, now=_NOW) is None, (
        "an expired handshake cannot be completed"
    )


async def test_flow_is_erased_with_its_request_row(db_session: AsyncSession) -> None:
    flow = await _seed(db_session)
    deleted = await requests_store.delete_credential_requests_for_platform_user(
        db_session, platform_user_id="requester-1", tenant_id=flow.tenant_id
    )
    assert deleted == 1, "the request row is the erasure anchor"
    assert await store.get_flow(db_session, state=flow.state) is None, (
        "the flow must cascade from its request row so purge stays complete"
    )


async def test_list_completed_grants_lists_only_stored_grants(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    connected = await make_account(db_session, tenant=tenant)
    decliner = await make_account(db_session, tenant=tenant)
    abandoned = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()

    async def flow_for(account_id: uuid.UUID, requester: str) -> McpOAuthFlowRow:
        request = await requests_store.create_credential_request(
            db_session,
            token=mint_request_token(),
            kind="mcp_oauth",
            tenant_id=tenant.id,
            agent_id=agent_id,
            account_id=account_id,
            target="docs",
            mcp_server_url="https://mcp.example.com/docs",
            requester_platform_user_id=requester,
            channel_id="chan-1",
            expires_at=_NOW + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="ag_test",
            target_name="daimon",
            requested_work=None,
        )
        return await store.create_flow(
            db_session,
            state="st_" + uuid.uuid4().hex,
            request_token=request.token,
            tenant_id=tenant.id,
            account_id=account_id,
            agent_id=agent_id,
            server_name="docs",
            mcp_server_url="https://mcp.example.com/docs",
            redirect_uri="https://d.example/oauth/mcp/callback",
            code_verifier="verifier",
            expires_at=_NOW + timedelta(minutes=10),
        )

    signed_in = await flow_for(connected.id, "requester-connected")
    declined = await flow_for(decliner.id, "requester-decliner")
    await flow_for(abandoned.id, "requester-abandoned")
    await store.consume_flow(db_session, state=signed_in.state, now=_NOW)
    await store.mark_flow_completed(db_session, state=signed_in.state, now=_NOW)
    # The decline path spends the row and stops: `used_at` alone is not a grant.
    await store.consume_flow(db_session, state=declined.state, now=_NOW)

    server_url = "https://mcp.example.com/docs"
    grants = await store.list_completed_grants(
        db_session, tenant_id=tenant.id, server_urls=[server_url + "/"]
    )
    assert [grant.account_id for grant in grants] == [connected.id], (
        "only the account whose grant was stored is connected, trailing slash or not"
    )
    assert grants[0].agent_id == agent_id, (
        "the row says which agent the sign-in was for — a fork of it holds no grant"
    )
    assert (
        await store.list_completed_grants(
            db_session, tenant_id=tenant.id, server_urls=["https://mcp.example.com/other"]
        )
        == ()
    ), "a URL nobody signed in to reads nothing, however many sign-ins the tenant has"
    other_tenant = await make_tenant(db_session)
    assert (
        await store.list_completed_grants(
            db_session, tenant_id=other_tenant.id, server_urls=[server_url]
        )
        == ()
    ), "grants never cross a tenant boundary"
