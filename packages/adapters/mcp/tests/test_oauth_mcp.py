"""`/oauth/mcp/start` and `/oauth/mcp/callback` against real Postgres, a mocked
authorization server and a fake Managed Agents.

The request rows are seeded without a posted card on purpose: the callback's
card edit would otherwise try to log the mcp process into Discord, and the
card's words are covered by the posted-controls suites.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from daimon.adapters.mcp.oauth_mcp import _success_page, build_oauth_mcp_routes
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._credential_button import (
    edit_card_state,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from daimon.core.continuity.messages import ConfigurationChange
from daimon.core.credential_requests import mint_request_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_oauth import begin_mcp_oauth_flow
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores.domain import CredentialRequestRow, McpOAuthFlowRow
from daimon.testing import ma_agent
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, json_body, list_response
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
_MCP_URL = "https://mcp.notion.com/mcp"
_ROOT = "https://daimon.example"
_AS = {
    "issuer": "https://mcp.notion.com",
    "authorization_endpoint": "https://mcp.notion.com/authorize",
    "token_endpoint": "https://mcp.notion.com/token",
    "registration_endpoint": "https://mcp.notion.com/register",
    "token_endpoint_auth_methods_supported": ["none"],
}


@pytest.mark.parametrize(
    ("platform", "name", "url"),
    [
        ("discord", "Discord", "https://discord.com/app"),
        ("slack", "Slack", "https://app.slack.com/client/"),
        ("teams", "Teams", "https://teams.microsoft.com/"),
    ],
)
def test_oauth_success_returns_to_platform(platform: str, name: str, url: str) -> None:
    page = _success_page(server_name="Research API", agent_name="ResearchBot", platform=platform)
    body = page.body.decode()
    assert f'href="{url}"' in body
    assert f"Back to {name}" in body
    assert body.count('class="gh-primary"') == 1
    if platform == "discord":
        assert 'class="web-icon web-icon--discord"' in body
    if platform == "teams":
        assert 'class="web-icon web-icon--teams"' not in body


def _settings() -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(f"{_ROOT}/mcp")),
    )


def _notion(
    token_forms: list[dict[str, list[str]]],
    *,
    registrations: list[str] | None = None,
    token_body: dict[str, str] | None = None,
) -> httpx.MockTransport:
    """The authorization server: discovery, registration and the token endpoint."""
    registered = registrations if registrations is not None else []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path == "/mcp":
            return httpx.Response(401, headers={"WWW-Authenticate": "Bearer"})
        if path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(404)
        if path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={
                    "resource": "https://mcp.notion.com",
                    "authorization_servers": [_AS["issuer"]],
                },
            )
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=_AS)
        if req.method == "POST" and path == "/register":
            registered.append(f"cid{len(registered) + 1}")
            return httpx.Response(
                201, json={"client_id": registered[-1], "token_endpoint_auth_method": "none"}
            )
        if req.method == "POST" and path == "/token":
            token_forms.append(parse_qs(req.content.decode()))
            body = (
                token_body
                if token_body is not None
                else {"access_token": "at", "refresh_token": "rt"}
            )
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _fake_ma(
    tenant_id: uuid.UUID,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_present: bool = True,
    vault_write_status: int = 200,
) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    created: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    vault_id = "vlt_me"
    agent = ma_agent(
        id="ag_oauth",
        name="daimon",
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "daimon"},
        tools=[
            {
                "type": "agent_toolset_20260401",
                "configs": [],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            }
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
                    "display_name": f"daimon-mcp:{account_id}:{agent_id}",
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
                    "auth": {"type": "static_bearer", "mcp_server_url": f"{_ROOT}/mcp"},
                    "created_at": "2026-09-01T00:00:00Z",
                    "updated_at": "2026-09-01T00:00:00Z",
                    "archived_at": None,
                    "display_name": None,
                    "metadata": {"daimon_chat_identity": str(agent_id)},
                }
            ]
        ),
    )

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        if vault_write_status != 200:
            return httpx.Response(vault_write_status, json={"error": {"type": "api_error"}})
        created.append(json_body(req))
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

    router.add("POST", rf"/v1/vaults/{vault_id}/credentials", on_create)
    if agent_present:
        router.add_agent_list(agent)
        router.add_agent(agent)
    else:
        router.add_agent_list()

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        updates.append(json_body(req))
        return httpx.Response(200, json=agent.model_dump(mode="json"))

    router.add("POST", r"/v1/agents/ag_oauth", on_update)
    return build_fake_anthropic(router.dispatch), created, updates


async def _seed_flow(session: AsyncSession) -> tuple[McpOAuthFlowRow, uuid.UUID]:
    tenant = await make_tenant(session)
    account = await make_account(session, tenant=tenant)
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
    flow = await begin_mcp_oauth_flow(
        session, request=request, app_root_url=_ROOT, now=_NOW, state="st_" + uuid.uuid4().hex
    )
    return flow, tenant.id


def _app(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    anthropic: Any,
    transport: httpx.MockTransport,
    now: datetime = _NOW,
) -> Starlette:
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=anthropic,
        settings=_settings(),
        deployment_default=DeploymentDefault(),
        fernet=make_fernet(),
    )
    assert runtime.fernet is not None
    start, callback = build_oauth_mcp_routes(
        runtime=runtime,
        fernet=runtime.fernet,
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
        now=lambda: now,
    )
    return Starlette(
        routes=[
            Route("/oauth/mcp/start", start, methods=["GET"]),
            Route("/oauth/mcp/callback", callback, methods=["GET"]),
        ]
    )


async def test_start_registers_a_client_and_redirects_to_the_authorization_server(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    flow, tenant_id = await _seed_flow(db_session)
    await db_session.commit()
    anthropic, _created, _updates = _fake_ma(
        tenant_id, account_id=flow.account_id, agent_id=flow.agent_id
    )
    app = _app(db_session_factory, anthropic=anthropic, transport=_notion([]))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(f"/oauth/mcp/start?state={flow.state}")

    assert r.status_code == 302, f"start must redirect; got {r.status_code}: {r.text}"
    location = urlparse(r.headers["location"])
    assert location.path == "/authorize" and location.netloc == "mcp.notion.com"
    query = parse_qs(location.query)
    assert query["client_id"] == ["cid1"] and query["state"] == [flow.state]
    assert query["redirect_uri"] == [f"{_ROOT}/oauth/mcp/callback"]
    async with db_session_factory() as session:
        saved = await flows_store.get_flow(session, state=flow.state)
    assert saved is not None and saved.client_id == "cid1", "the registered client is on the row"


async def test_start_answers_an_unknown_or_spent_state_with_the_expired_page(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    flow, tenant_id = await _seed_flow(db_session)
    await flows_store.consume_flow(db_session, state=flow.state, now=_NOW)
    await db_session.commit()
    anthropic, _c, _u = _fake_ma(tenant_id, account_id=flow.account_id, agent_id=flow.agent_id)
    app = _app(db_session_factory, anthropic=anthropic, transport=_notion([]))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        spent = await client.get(f"/oauth/mcp/start?state={flow.state}")
        unknown = await client.get("/oauth/mcp/start?state=nope")

    assert spent.status_code == 400 and "expired" in spent.text
    assert unknown.status_code == 400, "an unknown state must not leak whether it ever existed"


async def test_callback_stores_the_grant_attaches_the_server_and_is_single_use(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    flow, tenant_id = await _seed_flow(db_session)
    await db_session.commit()
    anthropic, created, updates = _fake_ma(
        tenant_id, account_id=flow.account_id, agent_id=flow.agent_id
    )
    token_forms: list[dict[str, list[str]]] = []
    app = _app(db_session_factory, anthropic=anthropic, transport=_notion(token_forms))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/oauth/mcp/start?state={flow.state}")
        done = await client.get(f"/oauth/mcp/callback?code=code123&state={flow.state}")
        replay = await client.get(f"/oauth/mcp/callback?code=code123&state={flow.state}")

    assert done.status_code == 200 and "Connected notion" in done.text, done.text
    assert token_forms[0]["code"] == ["code123"], "the callback's code is exchanged"
    assert created[0]["auth"]["type"] == "mcp_oauth", "the grant is an mcp_oauth credential"
    assert [s["name"] for s in updates[0]["mcp_servers"]] == ["notion"], "the server is attached"
    assert replay.status_code == 400, "a replayed callback finds the flow already spent"
    async with db_session_factory() as session:
        request = await requests_store.peek_credential_request(session, token=flow.request_token)
    assert request is not None and request.outcome == "applied", "the request records its outcome"


async def test_callback_with_a_provider_error_declines_without_touching_ma(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    flow, tenant_id = await _seed_flow(db_session)
    await db_session.commit()
    anthropic, created, updates = _fake_ma(
        tenant_id, account_id=flow.account_id, agent_id=flow.agent_id
    )
    app = _app(db_session_factory, anthropic=anthropic, transport=_notion([]))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(f"/oauth/mcp/callback?error=access_denied&state={flow.state}")

    assert r.status_code == 200 and "cancelled" in r.text
    assert created == [] and updates == [], "a declined sign-in stores and attaches nothing"
    body = json.dumps({"created": created})
    assert "mcp_oauth" not in body
    async with db_session_factory() as session:
        request = await requests_store.peek_credential_request(session, token=flow.request_token)
    assert request is not None and request.outcome == "declined", "the card does not stay pending"


async def test_callback_for_a_deleted_agent_stores_the_grant_but_says_nothing_was_attached(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    flow, tenant_id = await _seed_flow(db_session)
    await db_session.commit()
    anthropic, created, updates = _fake_ma(
        tenant_id, account_id=flow.account_id, agent_id=flow.agent_id, agent_present=False
    )
    app = _app(db_session_factory, anthropic=anthropic, transport=_notion([]))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/oauth/mcp/start?state={flow.state}")
        r = await client.get(f"/oauth/mcp/callback?code=code123&state={flow.state}")

    assert r.status_code == 200 and "agent is gone" in r.text, r.text
    assert "Connected notion" not in r.text, "no success page for a server nothing uses"
    assert len(created) == 1 and updates == [], "the grant is kept; there is no agent to attach"
    async with db_session_factory() as session:
        request = await requests_store.peek_credential_request(session, token=flow.request_token)
    assert request is not None and request.outcome == "write_failed"


async def test_editing_the_card_without_a_discord_bot_token_does_not_raise(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The grant is stored before the card is edited; a deployment whose mcp
    process has no Discord token must not turn that into a failed callback."""
    runtime = McpRuntime(
        session_factory=db_session_factory,
        client=build_fake_anthropic(MARouter().dispatch),
        settings=_settings(),
        deployment_default=DeploymentDefault(),
        fernet=make_fernet(),
    )
    assert runtime.settings.discord is None, "the mcp process here has no Discord token"
    row = CredentialRequestRow(
        token="crq_x",
        kind="mcp_oauth",
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        target="notion",
        mcp_server_url=_MCP_URL,
        requester_platform_user_id="1",
        channel_id="2",
        platform="discord",
        origin_thread_id="2",
        posted_message_id="3",
        idempotency_key=uuid.uuid4(),
        created_at=_NOW,
        expires_at=_NOW + timedelta(minutes=30),
        used_at=_NOW,
    )
    await edit_card_state(
        runtime,
        row=row,
        state="applied",
        outcome=ConfigurationChange(
            target_name="daimon", kind="mcp", availability="next_message", detail="notion"
        ),
    )


async def test_opening_the_start_link_twice_registers_one_client(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The code the provider issues must be exchanged as the client that asked for it."""
    flow, tenant_id = await _seed_flow(db_session)
    await db_session.commit()
    anthropic, _c, _u = _fake_ma(tenant_id, account_id=flow.account_id, agent_id=flow.agent_id)
    registrations: list[str] = []
    app = _app(
        db_session_factory, anthropic=anthropic, transport=_notion([], registrations=registrations)
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.get(f"/oauth/mcp/start?state={flow.state}")
        second = await client.get(f"/oauth/mcp/start?state={flow.state}")

    assert registrations == ["cid1"], "the second open registers nothing new"
    ids = [parse_qs(urlparse(r.headers["location"]).query)["client_id"] for r in (first, second)]
    assert ids == [["cid1"], ["cid1"]], "both redirects carry the same client"


async def test_callback_with_an_unusable_token_body_settles_the_card_instead_of_crashing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    flow, tenant_id = await _seed_flow(db_session)
    await db_session.commit()
    anthropic, created, _u = _fake_ma(tenant_id, account_id=flow.account_id, agent_id=flow.agent_id)
    app = _app(
        db_session_factory,
        anthropic=anthropic,
        transport=_notion([], token_body={"token_type": "bearer"}),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/oauth/mcp/start?state={flow.state}")
        r = await client.get(f"/oauth/mcp/callback?code=code123&state={flow.state}")

    assert r.status_code == 502 and "did not complete" in r.text, r.text
    assert created == [], "nothing is stored without an access token"
    async with db_session_factory() as session:
        request = await requests_store.peek_credential_request(session, token=flow.request_token)
    assert request is not None and request.outcome == "write_failed", "the card is not left pending"


async def test_callback_settles_the_card_when_managed_agents_refuses_the_vault_write(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An MA blip after the exchange is the exchange_failed page, not a traceback."""
    flow, tenant_id = await _seed_flow(db_session)
    await db_session.commit()
    anthropic, created, updates = _fake_ma(
        tenant_id, account_id=flow.account_id, agent_id=flow.agent_id, vault_write_status=503
    )
    app = _app(db_session_factory, anthropic=anthropic, transport=_notion([]))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/oauth/mcp/start?state={flow.state}")
        r = await client.get(f"/oauth/mcp/callback?code=code123&state={flow.state}")

    assert r.status_code == 502 and "did not complete" in r.text, r.text
    assert created == [] and updates == [], "nothing was stored or attached"
    async with db_session_factory() as session:
        request = await requests_store.peek_credential_request(session, token=flow.request_token)
    assert request is not None and request.outcome == "write_failed", "the card is not left pending"


@pytest.mark.parametrize("requester_is_admin", [False, True], ids=["member", "admin"])
async def test_a_pin_added_between_the_click_and_the_callback_stops_the_connection(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    requester_is_admin: bool,
) -> None:
    """The flow was started for an unpinned agent; by the callback it is pinned to a
    channel the request didn't come from. No grant, no attach, unless an admin."""
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy
    from daimon.core.stores.accounts import set_role
    from daimon.core.stores.domain import Role

    flow, tenant_id = await _seed_flow(db_session)
    await db_session.commit()
    anthropic, created, updates = _fake_ma(
        tenant_id, account_id=flow.account_id, agent_id=flow.agent_id
    )
    app = _app(db_session_factory, anthropic=anthropic, transport=_notion([]))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/oauth/mcp/start?state={flow.state}")
        async with db_session_factory.begin() as session:
            await set_access_policy(
                session,
                tenant_id=tenant_id,
                policy=TenantAccessPolicy(agent_channel_pins={"daimon": ("C_PINNED",)}),
            )
            if requester_is_admin:
                await set_role(session, flow.account_id, Role.ADMIN)
        done = await client.get(f"/oauth/mcp/callback?code=code123&state={flow.state}")

    if requester_is_admin:
        assert done.status_code == 200 and "Connected notion" in done.text, done.text
        assert created and updates
    else:
        assert done.status_code == 403 and "runs only in certain channels" in done.text, done.text
        assert created == [], "no grant is stored"
        assert updates == [], "nothing is attached"


async def test_a_pin_added_during_the_code_exchange_stores_no_grant(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Model gap G_oauth_check_before_write: the access rule is asked again after
    the token exchange and before anything reaches the vault, so a pin that lands
    while the exchange is in flight stores no grant and attaches nothing."""
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy

    flow, tenant_id = await _seed_flow(db_session)
    await db_session.commit()
    anthropic, created, updates = _fake_ma(
        tenant_id, account_id=flow.account_id, agent_id=flow.agent_id
    )
    inner = _notion([])

    async def pin_during_exchange(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/token":
            async with db_session_factory.begin() as session:
                await set_access_policy(
                    session,
                    tenant_id=tenant_id,
                    policy=TenantAccessPolicy(agent_channel_pins={"daimon": ("C_PINNED",)}),
                )
        return await inner.handle_async_request(req)

    app = _app(
        db_session_factory, anthropic=anthropic, transport=httpx.MockTransport(pin_during_exchange)
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/oauth/mcp/start?state={flow.state}")
        done = await client.get(f"/oauth/mcp/callback?code=code123&state={flow.state}")

    assert done.status_code == 403 and "runs only in certain channels" in done.text, done.text
    assert created == [], "no grant is stored"
    assert updates == [], "nothing is attached"
