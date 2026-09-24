"""A hub ask creates its MA session as the caller's account in the addressed tenant,
the same attribution a chat driver's turn carries. MCP turns record no usage row today
(that write happens only in the Discord/Slack SSE-driven ``run_turn`` path), so billing
parity itself isn't something this test can verify -- only the attribution that would
feed it."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaManagedAgentsSession,
)
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event import (
    BetaManagedAgentsUserMessageEvent,
)
from daimon.adapters.mcp.hub.app import build_hub_app
from daimon.adapters.mcp.hub.claims import encode_hub_claims
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    HubSettings,
    McpSettings,
    Settings,
)
from daimon.core.defaults.loader import DeploymentDefault
from daimon.core.hub_identity import HubTenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.testing import AGENT_ID, build_turn_router, ma_session
from daimon.testing.factories import make_ledger_entry, make_platform_principal, make_tenant
from daimon.testing.ma import (
    build_fake_anthropic,
    session_response,
)
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette

from .test_app import _lifespan

_TOKEN = "tok"


def _runtime(client: AsyncAnthropic, session_factory: Any) -> McpRuntime:
    """Mirrors ``test_tools._runtime`` but pins ``environment_name`` to
    ``"test-env"``, matching the environment tag ``build_turn_router`` bakes into
    its fake MA data. ``test_tools._runtime``'s ``"production"`` default is fine
    over there because none of those tests drive environment resolution; this
    one runs a real turn through ``_ask_impl``, which does.
    """
    settings = Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(public_url=None, jwt_secret=None),
        hub=HubSettings(),
    )
    return McpRuntime(
        session_factory=session_factory,
        client=client,  # type: ignore[arg-type]
        settings=settings,
        deployment_default=DeploymentDefault(environment_name="test-env"),
    )


async def _call_tool(
    app: Starlette, path: str, token: str, *, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Drive one MCP tool call over HTTP through the real hub app, auth middleware included."""
    async with (
        _lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        init = await c.post(
            path,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
        )
        assert init.status_code == 200, init.text
        resp = await c.post(
            path,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()  # type: ignore[return-value]


def _fake_session_for(kwargs: dict[str, Any]) -> BetaManagedAgentsSession:
    """A ``BetaManagedAgentsSession`` shaped like ``create_session``'s real return,
    with ``agent.id`` pinned to ``AGENT_ID`` so ``_verify_agent_owns_session``'s
    ownership check passes."""
    return ma_session(
        id="ses_hub_parity", agent_id=AGENT_ID, environment_id="env_parity_test", status="running"
    )


async def test_hub_ask_creates_session_under_callers_account_in_that_tenant(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="u1", tenant=tenant
    )
    # A hub ask runs through the real admission gate (_admit), which denies a
    # zero-balance tenant. Top up so the turn actually reaches create_session.
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await db_session.commit()

    hub_tenant = HubTenant(
        tenant_id=tenant.id,
        account_id=principal.account_id,
        workspace_id="g1",
        workspace_name="PyMC",
    )
    claims = encode_hub_claims(platform="discord", platform_user_id="u1", tenants=[hub_tenant])
    auth = StaticTokenVerifier(
        tokens={_TOKEN: {"sub": "u1", "client_id": "c", "upstream_claims": claims}}
    )

    router = build_turn_router(
        str(tenant.id),
        send_events_data=[
            BetaManagedAgentsUserMessageEvent(
                id="sevt_hub_boundary",
                content=[BetaManagedAgentsTextBlock(type="text", text="hello")],
                type="user.message",
                processed_at=datetime(2026, 9, 6, tzinfo=UTC),
            ).model_dump(mode="json")
        ],
    )
    # build_turn_router wires the SSE-stream turn path the Discord/Slack driver
    # consumes; the MCP ask tool instead polls GET /v1/sessions/{id} and reads
    # GET /v1/sessions/{id}/events directly, as the round-trip test in
    # test_agent_chat.py does, so those two routes are added on top.
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)$",
        # Tagged with the caller's account, as create_session tags a real one.
        lambda _r, m: session_response(
            session_id=m.group(1),
            status="idle",
            agent_id=AGENT_ID,
            metadata={"daimon_account": str(principal.account_id)},
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)/events$",
        lambda _r, _m: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "sevt_hub_msg",
                        "type": "agent.message",
                        "content": [{"type": "text", "text": "Hello from the agent!"}],
                    }
                ],
                "next_page": None,
            },
        ),
    )
    runtime = _runtime(build_fake_anthropic(router.dispatch), db_session_factory)
    mcp = build_hub_app(platform="discord", runtime=runtime, auth=auth, billing_config=None)

    daimon_id = str(derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=AGENT_ID))

    create_session = AsyncMock(side_effect=lambda *a, **kw: _fake_session_for(kw))
    with patch("daimon.adapters.mcp.tools.agent_chat.create_session", new=create_session):
        result = await _call_tool(
            mcp.http_app(path="/mcp", stateless_http=True, json_response=True),
            "/mcp",
            _TOKEN,
            name="ask",
            arguments={"daimon_id": daimon_id, "message": "status?"},
        )

    payload = result.get("result", result)
    assert isinstance(payload, dict) and not payload.get("isError"), (
        f"ask tool call over the hub app failed: {payload!r}"
    )

    create_session.assert_awaited_once()
    kwargs = create_session.await_args.kwargs
    assert kwargs["account_id"] == principal.account_id and kwargs["tenant_id"] == tenant.id, (
        f"session must be created as the caller's account in the addressed tenant, got {kwargs!r}"
    )
    assert kwargs["agent_uuid"] == uuid.UUID(daimon_id), (
        f"session must be tagged with the addressed daimon's derived uuid, got {kwargs!r}"
    )

    structured = payload.get("structuredContent") or {}
    assert structured.get("message") == "Hello from the agent!", (
        f"ask must surface the agent's reply text, got {payload!r}"
    )
