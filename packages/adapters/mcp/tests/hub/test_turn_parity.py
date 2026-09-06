"""A hub ask bills the same usage event a chat turn does, against the addressed tenant."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsSession,
)
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.sessions.beta_managed_agents_agent_message_event import (
    BetaManagedAgentsAgentMessageEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
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
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.hub_identity import HubTenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.testing.factories import make_ledger_entry, make_platform_principal, make_tenant
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    MARouter,
    build_fake_anthropic,
    list_response,
    send_events_response,
    session_response,
    sse_response,
)
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette

pytestmark = pytest.mark.asyncio

_TOKEN = "tok"

# build_turn_router and its constants are copied from tests/parity/conftest.py
# (repo root) rather than imported: this suite runs under
# `--import-mode=importlib` and, collected on its own (`pytest
# packages/adapters/mcp/tests/hub`), never gives pytest a reason to add the
# repo root to sys.path, so `from tests.parity.conftest import ...` is not
# resolvable here. Keep this in sync with the original if it changes.
AGENT_TEXT = "Hello from the agent!"
AGENT_ID = "ag_parity_test"
ENV_ID = "env_parity_test"
MODEL_ID = "claude-sonnet-4-6"


def build_turn_router(
    tenant_id_str: str,
    *,
    agent_id: str = AGENT_ID,
    env_id: str = ENV_ID,
    model_id: str = MODEL_ID,
    agent_text: str = AGENT_TEXT,
    usage_event_id: str = "evt_parity_usage",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> MARouter:
    """Build a MARouter handling agent/environment resolution + a turn SSE stream.

    Copied verbatim from ``tests/parity/conftest.py`` -- see the module
    docstring above for why this isn't an import.
    """
    agent_item = BetaManagedAgentsAgent(
        id=agent_id,
        type="agent",
        name="test-agent",
        model=BetaManagedAgentsModelConfig(id=model_id),
        metadata={
            MA_METADATA_KEY_TENANT: tenant_id_str,
            MA_METADATA_KEY_NAME: "test-agent",
        },
        description=None,
        created_at="2026-06-14T00:00:00Z",  # pyright: ignore[reportArgumentType]
        updated_at="2026-06-14T00:00:00Z",  # pyright: ignore[reportArgumentType]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    env_item = BetaEnvironment(
        id=env_id,
        type="environment",
        name="test-env",
        config=EMPTY_CLOUD_CONFIG,
        metadata={
            MA_METADATA_KEY_TENANT: tenant_id_str,
            MA_METADATA_KEY_NAME: "test-env",
        },
        description="",
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:00:00Z",
    ).model_dump(mode="json")

    now = datetime.now(UTC)
    agent_message_event = BetaManagedAgentsAgentMessageEvent(
        id="evt_parity_msg",
        type="agent.message",
        processed_at=now,
        content=[BetaManagedAgentsTextBlock(type="text", text=agent_text)],
    ).model_dump(mode="json")

    model_request_end_event = BetaManagedAgentsSpanModelRequestEndEvent(
        id=usage_event_id,
        is_error=False,
        model_request_start_id="start_parity",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        processed_at=now,
        type="span.model_request_end",
    ).model_dump(mode="json")

    idle_event = BetaManagedAgentsSessionStatusIdleEvent(
        id="evt_parity_idle",
        type="session.status_idle",
        processed_at=now,
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
    ).model_dump(mode="json")

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([agent_item]))
    router.add(
        "GET",
        r"/v1/agents/[^/]+",
        lambda req, _m: httpx.Response(200, json=agent_item),
    )
    router.add("GET", r"/v1/environments", lambda req, _m: list_response([env_item]))
    router.add(
        "GET",
        r"/v1/environments/[^/]+",
        lambda req, _m: httpx.Response(200, json=env_item),
    )
    router.add(
        "POST",
        r"/v1/sessions/[^/]+/events",
        lambda req, _m: send_events_response(
            data=[
                BetaManagedAgentsUserMessageEvent(
                    id="sevt_hub_boundary",
                    content=[BetaManagedAgentsTextBlock(type="text", text="hello")],
                    type="user.message",
                    processed_at=datetime(2026, 9, 6, tzinfo=UTC),
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events/stream",
        lambda req, _m: sse_response([agent_message_event, model_request_end_event, idle_event]),
    )
    return router


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


@asynccontextmanager
async def _lifespan(app: Starlette):
    async with app.router.lifespan_context(app):
        yield


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
    """A ``BetaManagedAgentsSession`` shaped like ``create_session``'s real return.

    Same shape ``tests/tools/test_agent_chat.py::_make_fake_session`` builds
    (~line 116), except ``agent.id`` is pinned to ``AGENT_ID`` so
    ``_verify_agent_owns_session``'s ownership check passes.
    """
    return BetaManagedAgentsSession.model_validate(
        {
            "id": "ses_hub_parity",
            "type": "session",
            "agent": {
                "id": AGENT_ID,
                "name": "test-agent",
                "version": 1,
                "type": "agent",
                "model": {"id": "claude-sonnet-4-6"},
                "mcp_servers": [],
                "skills": [],
                "tools": [],
            },
            "archived_at": None,
            "created_at": "2026-06-23T00:00:00Z",
            "updated_at": "2026-06-23T00:00:00Z",
            "outcome_evaluations": [],
            "environment_id": "env_parity_test",
            "metadata": {},
            "resources": [],
            "stats": {},
            "status": "running",
            "title": None,
            "usage": {},
            "vault_ids": [],
        }
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

    router = build_turn_router(str(tenant.id))
    # build_turn_router wires the SSE-stream turn path the Discord/Slack driver
    # consumes; the MCP ask tool instead polls GET /v1/sessions/{id} and reads
    # GET /v1/sessions/{id}/events directly, as the round-trip test in
    # test_agent_chat.py does, so those two routes are added on top.
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)$",
        lambda _r, m: session_response(session_id=m.group(1), status="idle", agent_id=AGENT_ID),
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
