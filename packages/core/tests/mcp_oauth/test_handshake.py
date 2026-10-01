"""`daimon.core.mcp_oauth.handshake`: minting the flow and preparing the redirect."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from daimon.core.credential_requests import mint_request_token
from daimon.core.github_credentials import decrypt_token
from daimon.core.mcp_oauth.flow import generate_pkce
from daimon.core.mcp_oauth.handshake import (
    begin_mcp_oauth_flow,
    callback_url,
    prepare_authorization,
    start_url,
)
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores.domain import CredentialRequestRow
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
_ROOT = "https://daimon.example"
_MCP_URL = "https://mcp.notion.com/mcp"
_AS = {
    "issuer": "https://mcp.notion.com",
    "authorization_endpoint": "https://mcp.notion.com/authorize",
    "token_endpoint": "https://mcp.notion.com/token",
    "registration_endpoint": "https://mcp.notion.com/register",
    "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
}


async def _request(session: AsyncSession, *, kind: str = "mcp_oauth") -> CredentialRequestRow:
    tenant = await make_tenant(session)
    account = await make_account(session, tenant=tenant)
    return await requests_store.create_credential_request(
        session,
        token=mint_request_token(),
        kind=kind,  # type: ignore[arg-type]  # the invalid-kind test needs a wrong value
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        account_id=account.id,
        target="notion",
        mcp_server_url=_MCP_URL,
        requester_platform_user_id="requester-1",
        channel_id="chan-1",
        expires_at=_NOW + timedelta(minutes=30),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_test",
        target_name="daimon",
        requested_work=None,
    )


def test_start_and_callback_urls_hang_off_the_app_root() -> None:
    assert start_url(_ROOT + "/", state="a b") == f"{_ROOT}/oauth/mcp/start?state=a%20b"
    assert callback_url(_ROOT) == f"{_ROOT}/oauth/mcp/callback"


async def test_begin_flow_binds_the_requesters_identity_and_a_verifier(
    db_session: AsyncSession,
) -> None:
    request = await _request(db_session)
    flow = await begin_mcp_oauth_flow(
        db_session, request=request, app_root_url=_ROOT, now=_NOW, state="st_1"
    )
    assert (flow.tenant_id, flow.account_id, flow.agent_id) == (
        request.tenant_id,
        request.account_id,
        request.agent_id,
    ), "the grant must land in the requester's own vault, so the flow carries their identity"
    assert flow.redirect_uri == f"{_ROOT}/oauth/mcp/callback"
    assert len(flow.code_verifier) >= 43, "a PKCE verifier is minted per flow"
    assert flow.expires_at == _NOW + timedelta(minutes=10)


async def test_begin_flow_refuses_a_request_of_another_kind(db_session: AsyncSession) -> None:
    request = await _request(db_session, kind="mcp")
    with pytest.raises(ValueError, match="does not start an OAuth flow"):
        await begin_mcp_oauth_flow(db_session, request=request, app_root_url=_ROOT, now=_NOW)


async def test_prepare_authorization_registers_a_client_and_builds_the_redirect(
    db_session: AsyncSession,
) -> None:
    request = await _request(db_session)
    flow = await begin_mcp_oauth_flow(
        db_session, request=request, app_root_url=_ROOT, now=_NOW, code_verifier="v" * 64
    )
    registrations: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path == "/mcp":
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.notion.com/.well-known/oauth-protected-resource"'
                },
            )
        if path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={
                    "resource": "https://mcp.notion.com",
                    "authorization_servers": ["https://mcp.notion.com"],
                    "scopes_supported": ["default"],
                },
            )
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=_AS)
        if req.method == "POST" and path == "/register":
            registrations.append(json.loads(req.content))
            return httpx.Response(
                201,
                json={
                    "client_id": "cid",
                    "client_secret": "sec",
                    "token_endpoint_auth_method": "client_secret_post",
                },
            )
        return httpx.Response(404)

    fernet = make_fernet()
    prepared = await prepare_authorization(
        db_session,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        flow=flow,
        fernet=fernet,
    )
    assert prepared is not None
    assert registrations[0]["redirect_uris"] == [f"{_ROOT}/oauth/mcp/callback"], (
        "the client is registered for daimon's callback"
    )
    query = parse_qs(urlparse(prepared.authorize_url).query)
    assert query["client_id"] == ["cid"] and query["state"] == [flow.state]
    assert query["code_challenge"] == [generate_pkce(verifier="v" * 64).code_challenge]
    assert query["scope"] == ["default"] and query["resource"] == ["https://mcp.notion.com"]

    saved = await flows_store.get_flow(db_session, state=flow.state)
    assert saved is not None and saved.token_endpoint == "https://mcp.notion.com/token"
    assert saved.client_secret_encrypted is not None
    assert decrypt_token(fernet, saved.client_secret_encrypted.encode()) == "sec", (
        "a confidential client's secret is stored encrypted, never in the clear"
    )
