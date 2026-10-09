"""`daimon.core.mcp_oauth.complete`: code exchange, vault write, agent attach."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from daimon.core.credential_requests import mint_request_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_oauth.complete import (
    McpOAuthIncompleteFlowError,
    complete_mcp_oauth_flow,
    registered_client,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import McpOAuthFlowRow, Role
from daimon.testing import ma_agent
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, json_body, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
_MCP_URL = "https://mcp.notion.com/mcp"
_PUBLIC_URL = "https://daimon.example/mcp"


async def _flow(
    session: AsyncSession, *, with_client: bool = True, admin: bool = False
) -> tuple[McpOAuthFlowRow, uuid.UUID]:
    tenant = await make_tenant(session)
    account = await make_account(session, tenant=tenant)
    if admin:
        await set_role(session, account.id, Role.ADMIN)
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_oauth")
    request = await requests_store.create_credential_request(
        session,
        token=mint_request_token(),
        kind="mcp_oauth",
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=account.id,
        target="notion",
        mcp_server_url=_MCP_URL,
        requester_platform_user_id="requester-1",
        channel_id="chan-1",
        expires_at=_NOW + timedelta(minutes=30),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_oauth",
        target_name="daimon",
        requested_work=None,
    )
    flow = await flows_store.create_flow(
        session,
        state="st_" + uuid.uuid4().hex,
        request_token=request.token,
        tenant_id=tenant.id,
        account_id=account.id,
        agent_id=agent_id,
        server_name="notion",
        mcp_server_url=_MCP_URL,
        redirect_uri="https://daimon.example/oauth/mcp/callback",
        code_verifier="v" * 64,
        expires_at=_NOW + timedelta(minutes=10),
    )
    if with_client:
        saved = await flows_store.save_flow_client(
            session,
            state=flow.state,
            client_id="cid",
            client_secret_encrypted=None,
            token_endpoint_auth_method="none",
            token_endpoint="https://mcp.notion.com/token",
            authorization_endpoint="https://mcp.notion.com/authorize",
            resource="https://mcp.notion.com",
            scope="default",
        )
        assert saved is not None
        flow = saved
    return flow, tenant.id


def _fake_ma(
    tenant_id: uuid.UUID,
    *,
    vault_id: str,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    mcp_servers: list[dict[str, str]] | None = None,
):  # test-local bundle
    """MA with the requester's vault already bootstrapped and one agent."""
    created_credentials: list[dict[str, Any]] = []
    agent_updates: list[dict[str, Any]] = []
    display = f"daimon-mcp:{account_id}:{agent_id}"
    agent = ma_agent(
        id="ag_oauth",
        name="daimon",
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "daimon"},
        mcp_servers=mcp_servers or [],
        tools=[
            *[
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": server["name"],
                    "configs": [],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                }
                for server in mcp_servers or []
            ],
            {
                "type": "agent_toolset_20260401",
                "configs": [],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            },
        ],
    )
    router = MARouter()
    router.add(
        "GET",
        r"/v1/vaults",
        lambda _r, _m: list_response(
            [
                {
                    "id": vault_id,
                    "type": "vault",
                    "display_name": display,
                    "metadata": None,
                    "archived_at": None,
                    "created_at": "2026-09-01T00:00:00Z",
                }
            ]
        ),
    )
    router.add(
        "GET",
        rf"/v1/vaults/{vault_id}/credentials",
        lambda _r, _m: list_response(
            [
                {
                    "id": "vcrd_jwt",
                    "type": "vault_credential",
                    "vault_id": vault_id,
                    "auth": {"type": "static_bearer", "mcp_server_url": _PUBLIC_URL},
                    "created_at": "2026-09-01T00:00:00Z",
                    "updated_at": "2026-09-01T00:00:00Z",
                    "archived_at": None,
                    "display_name": None,
                    "metadata": {"daimon_chat_identity": str(agent_id)},
                }
            ]
        ),
    )

    def on_create_credential(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json_body(req)
        created_credentials.append(body)
        return httpx.Response(
            200,
            json={
                "id": "vcrd_oauth",
                "type": "vault_credential",
                "vault_id": vault_id,
                "auth": {"type": "mcp_oauth", "mcp_server_url": _MCP_URL},
                "created_at": "2026-09-15T12:00:00Z",
                "updated_at": "2026-09-15T12:00:00Z",
                "archived_at": None,
                "display_name": None,
                "metadata": None,
            },
        )

    router.add("POST", rf"/v1/vaults/{vault_id}/credentials", on_create_credential)
    router.add_agent_list(agent)
    router.add_agent(agent)

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        agent_updates.append(json_body(req))
        return httpx.Response(200, json=agent.model_dump(mode="json"))

    router.add("POST", r"/v1/agents/ag_oauth", on_update)
    return build_fake_anthropic(router.dispatch), created_credentials, agent_updates


async def test_complete_exchanges_code_writes_grant_and_attaches_server(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    flow, tenant_id = await _flow(db_session, admin=True)
    await db_session.commit()
    token_forms: list[dict[str, list[str]]] = []

    def token_handler(req: httpx.Request) -> httpx.Response:
        assert str(req.url) == "https://mcp.notion.com/token"
        token_forms.append(parse_qs(req.content.decode()))
        return httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )

    anthropic, created, updates = _fake_ma(
        tenant_id, vault_id="vlt_me", account_id=flow.account_id, agent_id=flow.agent_id
    )
    completion = await complete_mcp_oauth_flow(
        httpx.AsyncClient(transport=httpx.MockTransport(token_handler)),
        anthropic,
        flow=flow,
        code="code123",
        fernet=make_fernet(),
        jwt_secret=b"x" * 32,
        public_url=_PUBLIC_URL,
        now=_NOW,
        session_factory=db_session_factory,
        default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )
    assert completion.vault_id == "vlt_me" and completion.credential_id == "vcrd_oauth"
    assert completion.ma_agent_id == "ag_oauth"
    assert token_forms[0]["code"] == ["code123"] and token_forms[0]["code_verifier"] == ["v" * 64]
    assert created[0]["auth"]["type"] == "mcp_oauth", "the grant is an mcp_oauth credential"
    assert created[0]["auth"]["refresh"]["refresh_token"] == "rt", "Anthropic can refresh it"
    assert [s["name"] for s in updates[0]["mcp_servers"]] == ["notion"], (
        "the server is attached to the agent so its toolset exists"
    )
    assert any(
        t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == "notion"
        for t in updates[0]["tools"]
    ), "the matching mcp_toolset is added alongside the server"
    grants = await flows_store.list_completed_grants(
        db_session, tenant_id=tenant_id, server_urls=[flow.mcp_server_url]
    )
    assert [(g.agent_id, g.account_id, g.mcp_server_url) for g in grants] == [
        (flow.agent_id, flow.account_id, flow.mcp_server_url)
    ], (
        "the stored grant marks this person — and only this person — as connected, "
        "which is what decides whose sessions mount the server"
    )


async def test_registered_client_refuses_a_flow_that_skipped_start(
    db_session: AsyncSession,
) -> None:
    flow, _tenant_id = await _flow(db_session, with_client=False)
    with pytest.raises(McpOAuthIncompleteFlowError):
        registered_client(flow, fernet=make_fernet())


async def test_complete_refuses_repointing_a_shared_agents_server_for_a_member(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """H2: the grant is personal, the attach is not. A member's callback must not
    repoint the live agent's existing `notion` at the flow's URL."""
    from daimon.core.mcp_attach import McpServerReplaceRefusedError

    flow, tenant_id = await _flow(db_session)
    await db_session.commit()

    def token_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})

    anthropic, _created, updates = _fake_ma(
        tenant_id,
        vault_id="vlt_me",
        account_id=flow.account_id,
        agent_id=flow.agent_id,
        mcp_servers=[{"name": "notion", "type": "url", "url": "https://real.example.com/mcp"}],
    )
    with pytest.raises(McpServerReplaceRefusedError):
        await complete_mcp_oauth_flow(
            httpx.AsyncClient(transport=httpx.MockTransport(token_handler)),
            anthropic,
            flow=flow,
            code="code123",
            fernet=make_fernet(),
            jwt_secret=b"x" * 32,
            public_url=_PUBLIC_URL,
            now=_NOW,
            session_factory=db_session_factory,
            default=DeploymentDefault(agent_name="daimon", environment_name="default"),
        )
    assert updates == [], "the agent's server must not be repointed"


async def test_complete_lets_an_admin_requester_repoint_a_shared_agents_server(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The callback re-decides against the requester's recorded role (possibly stale)."""
    from daimon.core.stores.accounts import set_role
    from daimon.core.stores.domain import Role

    flow, tenant_id = await _flow(db_session)
    await set_role(db_session, flow.account_id, Role.ADMIN)
    await db_session.commit()

    def token_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})

    anthropic, _created, updates = _fake_ma(
        tenant_id,
        vault_id="vlt_me",
        account_id=flow.account_id,
        agent_id=flow.agent_id,
        mcp_servers=[{"name": "notion", "type": "url", "url": "https://real.example.com/mcp"}],
    )
    completion = await complete_mcp_oauth_flow(
        httpx.AsyncClient(transport=httpx.MockTransport(token_handler)),
        anthropic,
        flow=flow,
        code="code123",
        fernet=make_fernet(),
        jwt_secret=b"x" * 32,
        public_url=_PUBLIC_URL,
        now=_NOW,
        session_factory=db_session_factory,
        default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )
    assert completion.ma_agent_id == "ag_oauth"
    assert [s["url"] for s in updates[0]["mcp_servers"]] == [flow.mcp_server_url]


@pytest.mark.parametrize("granted", [True, False], ids=["channel-admin", "member"])
async def test_complete_decides_a_repoint_as_the_request_did_for_a_channel_admin(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], granted: bool
) -> None:
    """The callback builds the requester from their stored platform id and role ids, so a
    channel admin allowed when asking is not refused after the token is already stored.
    A server admin set c1's default, so its admins hold the agent."""
    from daimon.core.mcp_attach import McpServerReplaceRefusedError
    from daimon.core.scope import ChannelScopeRef
    from daimon.core.stores.accounts import get_account, set_platform_role_ids
    from daimon.core.stores.channel_admins import set_channel_admins
    from daimon.core.stores.scoped_config_write import set_fields
    from daimon.core.stores.tenants import get_tenant
    from daimon.testing.factories import make_platform_principal

    flow, tenant_id = await _flow(db_session)
    tenant = await get_tenant(db_session, tenant_id)
    account = await get_account(db_session, flow.account_id)
    assert tenant is not None and account is not None, "the flow's tenant and account exist"
    await make_platform_principal(
        db_session, platform="discord", external_id="requester-1", tenant=tenant, account=account
    )
    await set_platform_role_ids(db_session, account.id, ["r1"])
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant_id, channel_id="c1"),
        tenant_id=tenant_id,
        agent_name="daimon",
        mode="agent",
        set_by_admin=True,
    )
    await set_channel_admins(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        channel_id="c1" if granted else "c9",
        role_ids=["r1"],
        user_ids=[],
        actor_account_id=None,
    )
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy

    await set_access_policy(
        db_session,
        tenant_id=tenant_id,
        policy=TenantAccessPolicy(agent_channel_pins={"daimon": ("c1",)}),
    )
    await db_session.commit()

    def token_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})

    anthropic, _created, updates = _fake_ma(
        tenant_id,
        vault_id="vlt_me",
        account_id=flow.account_id,
        agent_id=flow.agent_id,
        mcp_servers=[{"name": "notion", "type": "url", "url": "https://real.example.com/mcp"}],
    )

    async def complete() -> None:
        await complete_mcp_oauth_flow(
            httpx.AsyncClient(transport=httpx.MockTransport(token_handler)),
            anthropic,
            flow=flow,
            code="code123",
            fernet=make_fernet(),
            jwt_secret=b"x" * 32,
            public_url=_PUBLIC_URL,
            now=_NOW,
            session_factory=db_session_factory,
            default=DeploymentDefault(agent_name="other", environment_name="default"),
        )

    if not granted:
        with pytest.raises(McpServerReplaceRefusedError):
            await complete()
        assert updates == [], "an admin of another channel does not repoint c1's agent"
        return
    await complete()
    assert [s["url"] for s in updates[0]["mcp_servers"]] == [flow.mcp_server_url], (
        "the admin of the only channel the agent answers in repoints its server"
    )


async def test_member_oauth_cannot_attach_a_new_server_to_an_unbound_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A personal grant must not authorize a shared-spec write; withdraw it on refusal."""
    from anthropic import AsyncAnthropic
    from daimon.core.mcp_oauth.complete import McpOAuthWriteRefusedError

    flow, tenant_id = await _flow(db_session)
    await set_role(db_session, flow.account_id, Role.USER)
    await db_session.commit()
    fake, created, updates = _fake_ma(
        tenant_id, vault_id="vlt_me", account_id=flow.account_id, agent_id=flow.agent_id
    )
    inner = fake._client._transport
    deleted: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and request.url.path.endswith("/vcrd_oauth"):
            deleted.append(request.url.path)
            return httpx.Response(204)
        return await inner.handle_async_request(request)

    client = AsyncAnthropic(
        api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"access_token": "personal-grant"})
        )
    )
    with pytest.raises(McpOAuthWriteRefusedError):
        await complete_mcp_oauth_flow(
            http,
            client,
            flow=flow,
            code="code",
            fernet=make_fernet(),
            jwt_secret=b"x" * 32,
            public_url=_PUBLIC_URL,
            now=_NOW,
            session_factory=db_session_factory,
            default=DeploymentDefault(agent_name="other"),
        )
    assert len(created) == 1
    assert deleted == ["/v1/vaults/vlt_me/credentials/vcrd_oauth"]
    assert updates == [], "the personal sign-in never changes the unbound agent"
    assert not await flows_store.list_completed_grants(
        db_session, tenant_id=tenant_id, server_urls=[_MCP_URL]
    )


async def test_personal_oauth_for_an_existing_server_does_not_mutate_the_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    flow, tenant_id = await _flow(db_session)
    await db_session.commit()
    client, created, updates = _fake_ma(
        tenant_id,
        vault_id="vlt_me",
        account_id=flow.account_id,
        agent_id=flow.agent_id,
        mcp_servers=[{"name": "notion", "type": "url", "url": _MCP_URL}],
    )
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"access_token": "personal-grant"})
        )
    )
    completion = await complete_mcp_oauth_flow(
        http,
        client,
        flow=flow,
        code="code",
        fernet=make_fernet(),
        jwt_secret=b"x" * 32,
        public_url=_PUBLIC_URL,
        now=_NOW,
        session_factory=db_session_factory,
        default=DeploymentDefault(agent_name="daimon"),
    )
    assert completion.ma_agent_id == "ag_oauth" and len(created) == 1
    assert updates == [], "personal authentication must leave the shared spec untouched"
