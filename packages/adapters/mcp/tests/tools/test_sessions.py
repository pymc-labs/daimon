from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event import (
    BetaManagedAgentsUserMessageEvent,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.middleware.mcp_identity import (
    IdentityMiddleware,
    production_agent_id_resolver,
    production_internal_resolver,
    production_is_admin_resolver,
    production_role_resolver,
    production_subject_resolver,
    production_tenant_resolver,
)
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.sessions import (
    SessionEventOut,
    SessionInfo,
    _get_session_impl,
    _list_session_events_impl,
    _list_sessions_impl,
    _send_message_impl,
    register_sessions_tools,
)
from daimon.core.scope import DeploymentDefault
from daimon.testing.ma import (
    MARouter,
    build_fake_anthropic,
    json_body,
    list_response,
    send_events_response,
)
from factories import make_ma_agent
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Message

pytestmark = pytest.mark.asyncio


def _runtime(client: AsyncAnthropic) -> McpRuntime:
    return McpRuntime(
        session_factory=MagicMock(),
        client=client,  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )


# ---------------------------------------------------------------------------
# Full-HTTP-pipeline harness (copied from test_agent_chat.py:964-1086) — FastMCP
# output-schema validation only runs through mcp.http_app() + a real JSON-RPC
# tools/call; unit-calling the _impl functions bypasses it entirely.
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _lifespan(app: ASGIApp) -> AsyncIterator[None]:
    send_q: asyncio.Queue[Message] = asyncio.Queue()
    recv_q: asyncio.Queue[Message] = asyncio.Queue()

    async def receive() -> Message:
        return await recv_q.get()

    async def send(message: Message) -> None:
        await send_q.put(message)

    async def run() -> None:
        await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    task = asyncio.create_task(run())
    await recv_q.put({"type": "lifespan.startup"})
    msg = await send_q.get()
    assert msg["type"] == "lifespan.startup.complete", msg
    try:
        yield
    finally:
        await recv_q.put({"type": "lifespan.shutdown"})
        msg = await send_q.get()
        assert msg["type"] == "lifespan.shutdown.complete", msg
        await task


def _parse_jsonrpc(resp: httpx.Response) -> dict[str, object]:
    ct = resp.headers.get("content-type", "")
    if "text/event-stream" in ct:
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])  # type: ignore[return-value]
        raise AssertionError(f"No data line in SSE: {resp.text!r}")
    return resp.json()  # type: ignore[return-value]


async def _call_tool_via_http(
    app: ASGIApp, token: str, name: str, arguments: dict[str, object]
) -> dict[str, object]:
    """Initialize an MCP HTTP session and call tools/call; return the JSON-RPC result.

    Goes through the full server pipeline (auth -> IdentityMiddleware -> tool ->
    FastMCP OUTPUT VALIDATION) so it exercises the same output-schema check that
    rejects overly-strict schemas in prod — unlike _impl-level tests, which bypass it.
    """
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    async with _lifespan(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        init_resp = await c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            headers=headers,
        )
        assert init_resp.status_code == 200, f"initialize failed: {init_resp.text}"
        session_id = init_resp.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        call_resp = await c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers=headers,
        )
        assert call_resp.status_code == 200, f"tools/call failed: {call_resp.text}"
        return _parse_jsonrpc(call_resp)


def _sessions_mcp_app(client: AsyncAnthropic, token: str, claims: dict[str, str]) -> ASGIApp:
    """Assemble a minimal FastMCP app with the sessions tool group registered
    behind the real auth + identity middleware pipeline."""
    mock_sessionmaker: async_sessionmaker[AsyncSession] = MagicMock()  # type: ignore[assignment]
    mcp = FastMCP(name="sessions-schema-test", auth=StaticTokenVerifier(tokens={token: claims}))
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=production_subject_resolver,
            tenant_resolver=production_tenant_resolver,
            role_resolver=production_role_resolver,
            agent_id_resolver=production_agent_id_resolver,
            is_admin_resolver=production_is_admin_resolver,
            internal_resolver=production_internal_resolver,
            sessionmaker=mock_sessionmaker,
        )
    )
    register_sessions_tools(mcp, _runtime(client))
    return mcp.http_app()


def _make_session_payload(
    *, session_id: str = "ses_1", agent_id: str = "ag_a", status: str = "idle"
) -> dict[str, Any]:
    """Build a BetaManagedAgentsSession payload via the real SDK constructor,
    then override ``status`` — the SDK's Literal cannot carry a novel value
    through *validated construction*, even though its response-parsing path
    tolerates one at the transport boundary (mirrors test_agent_chat.py's
    ``_make_fake_session(status=...)``)."""
    payload = BetaManagedAgentsSession.model_validate(
        {
            "id": session_id,
            "type": "session",
            "agent": {
                "id": agent_id,
                "name": "demo",
                "version": 1,
                "type": "agent",
                "model": {"id": "claude-opus-4-5"},
                "mcp_servers": [],
                "skills": [],
                "tools": [],
            },
            "archived_at": None,
            "created_at": "2026-05-08T10:00:00Z",
            "updated_at": "2026-05-08T10:00:00Z",
            "environment_id": "env_1",
            "metadata": {},
            "resources": [],
            "stats": {},
            "status": "idle",
            "title": None,
            "usage": {},
            "vault_ids": [],
        }
    ).model_dump(mode="json")
    payload["status"] = status
    return payload


def _make_thread_idle_event_raw(*, stop_reason_type: str = "end_turn") -> dict[str, Any]:
    """Build a ``session.thread_status_idle`` event as a raw dict — the pinned
    SDK's ``BetaManagedAgentsSessionEvent`` union has no constructor for this
    variant, so validated construction is impossible by definition (mirrors
    test_agent_chat.py's ``_make_thread_idle_event``)."""
    return {
        "id": "sevt_thread_idle_001",
        "content": None,
        "type": "session.thread_status_idle",
        "processed_at": "2026-07-01T13:32:27.914598Z",
        "agent_name": "test-agent",
        "session_thread_id": "sthr_test001",
        "stop_reason": {"type": stop_reason_type},
    }


# ---------------------------------------------------------------------------
# CLB-08 regression tests — permissive projections through the full pipeline
# ---------------------------------------------------------------------------


async def test_get_session_admits_novel_status_string_through_fastmcp() -> None:
    """get_session must not output-validation-error when MA reports a session
    status the pinned SDK's Literal does not model (e.g. "paused").

    RED on main: SessionInfo.status: Literal["rescheduling", "running", "idle",
    "terminated"] rejects the whole payload with a FastMCP output-validation
    error (isError). Exercised through the HTTP pipeline because FastMCP
    output validation only runs there — unit-calling _get_session_impl
    bypasses the schema check entirely.
    """
    tenant_id = uuid.uuid4()
    token = "get-session-novel-status"
    claims = {
        "sub": str(uuid.uuid4()),
        "tenant_id": str(tenant_id),
        "role": "user",
        "client_id": "test",
    }

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200, json=_make_session_payload(session_id="ses_1", agent_id="ag_a", status="paused")
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    app = _sessions_mcp_app(client, token, claims)

    result = await _call_tool_via_http(app, token, "get_session", {"session_id": "ses_1"})

    payload = result.get("result", result)
    assert isinstance(payload, dict), f"unexpected tools/call shape: {result!r}"
    assert not payload.get("isError"), (
        f"get_session must not output-validation-error on a novel status string; got {payload!r}"
    )
    structured = payload.get("structuredContent") or {}
    assert structured.get("status") == "paused", (
        f"the novel status must survive in the tool output; got {structured!r}"
    )


async def test_list_session_events_admits_thread_status_events_through_fastmcp() -> None:
    """list_session_events must not output-validation-error on a
    session.thread_status_idle event — a variant the pinned SDK's
    discriminated union does not model.

    RED on main: Page[BetaManagedAgentsSessionEvent] re-validates
    cursor.data against the union on construction and rejects the whole page
    (the #214 failure mode, on the tenant tool surface).
    """
    tenant_id = uuid.uuid4()
    token = "list-events-novel-type"
    claims = {
        "sub": str(uuid.uuid4()),
        "tenant_id": str(tenant_id),
        "role": "user",
        "client_id": "test",
    }

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200, json=_make_session_payload(session_id="ses_1", agent_id="ag_a")
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: httpx.Response(
            200,
            json={"data": [_make_thread_idle_event_raw()], "next_page": None},
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    app = _sessions_mcp_app(client, token, claims)

    result = await _call_tool_via_http(app, token, "list_session_events", {"session_id": "ses_1"})

    payload = result.get("result", result)
    assert isinstance(payload, dict), f"unexpected tools/call shape: {result!r}"
    assert not payload.get("isError"), (
        f"list_session_events must not output-validation-error on a "
        f"thread_status_idle event; got {payload!r}"
    )
    structured = payload.get("structuredContent") or {}
    types = [ev.get("type") for ev in structured.get("items", [])]  # type: ignore[union-attr]
    assert "session.thread_status_idle" in types, (
        f"the thread_status_idle event must survive in the page; got types {types!r}"
    )


async def test_send_message_admits_thread_status_events_through_fastmcp() -> None:
    """send_message must not output-validation-error when MA's send response
    includes a session.thread_status_idle event — a variant the pinned SDK's
    BetaManagedAgentsSendSessionEvents union does not model.

    RED on main: the tool's BetaManagedAgentsSendSessionEvents return
    annotation re-validates the raw response body and rejects it wholesale.
    """
    tenant_id = uuid.uuid4()
    token = "send-message-novel-type"
    claims = {
        "sub": str(uuid.uuid4()),
        "tenant_id": str(tenant_id),
        "role": "user",
        "client_id": "test",
    }

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200, json=_make_session_payload(session_id="ses_1", agent_id="ag_a")
        ),
    )
    router.add(
        "POST",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: send_events_response(data=[_make_thread_idle_event_raw()]),
    )
    client = build_fake_anthropic(router.dispatch)
    app = _sessions_mcp_app(client, token, claims)

    result = await _call_tool_via_http(
        app, token, "send_message", {"session_id": "ses_1", "text": "hello"}
    )

    payload = result.get("result", result)
    assert isinstance(payload, dict), f"unexpected tools/call shape: {result!r}"
    assert not payload.get("isError"), (
        f"send_message must not output-validation-error on a thread_status_idle "
        f"event in the response; got {payload!r}"
    )


async def test_list_sessions_returns_only_tenant_scoped_sessions() -> None:
    tenant_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions",
        lambda _r, _m: list_response(
            [
                BetaManagedAgentsSession.model_validate(
                    {
                        "id": "ses_1",
                        "type": "session",
                        "agent": {
                            "id": "ag_a",
                            "name": "demo",
                            "version": 1,
                            "type": "agent",
                            "model": {"id": "claude-opus-4-5"},
                            "mcp_servers": [],
                            "skills": [],
                            "tools": [],
                        },
                        "archived_at": None,
                        "created_at": "2026-05-08T10:00:00Z",
                        "updated_at": "2026-05-08T10:00:00Z",
                        "environment_id": "env_1",
                        "metadata": {},
                        "resources": [],
                        "stats": {},
                        "status": "idle",
                        "title": None,
                        "usage": {},
                        "vault_ids": [],
                    }
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    result = await _list_sessions_impl(_runtime(client), auth, page=None, agent_name=None)

    assert len(result) == 1, "should drain one session for the tenant's only agent"
    assert result[0].id == "ses_1", "should expose the MA session id"
    assert result[0].agent_id == "ag_a", "agent_id should come from session.agent.id"
    assert isinstance(result[0], SessionInfo), "should return SessionInfo projections"


async def test_list_sessions_with_unknown_agent_name_raises() -> None:
    tenant_id = uuid.uuid4()

    router = MARouter()
    # No agents in the tenant -> find_agent_by_daimon_tag returns None.
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    with pytest.raises(ToolError, match="not found"):
        await _list_sessions_impl(_runtime(client), auth, page=None, agent_name="nope")


async def test_get_session_raises_when_session_belongs_to_other_tenant() -> None:
    tenant_id = uuid.uuid4()

    router = MARouter()
    # Tenant has agent ag_a; the requested session is owned by ag_other.
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=BetaManagedAgentsSession.model_validate(
                {
                    "id": "ses_x",
                    "type": "session",
                    "agent": {
                        "id": "ag_other",
                        "name": "other",
                        "version": 1,
                        "type": "agent",
                        "model": {"id": "claude-opus-4-5"},
                        "mcp_servers": [],
                        "skills": [],
                        "tools": [],
                    },
                    "archived_at": None,
                    "created_at": "2026-05-08T10:00:00Z",
                    "updated_at": "2026-05-08T10:00:00Z",
                    "environment_id": "env_1",
                    "metadata": {},
                    "resources": [],
                    "stats": {},
                    "status": "idle",
                    "title": None,
                    "usage": {},
                    "vault_ids": [],
                }
            ).model_dump(mode="json"),
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    with pytest.raises(ToolError, match="session not found"):
        await _get_session_impl(_runtime(client), auth, "ses_x")


async def test_get_session_returns_session_info_when_owned() -> None:
    tenant_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=BetaManagedAgentsSession.model_validate(
                {
                    "id": "ses_1",
                    "type": "session",
                    "agent": {
                        "id": "ag_a",
                        "name": "demo",
                        "version": 1,
                        "type": "agent",
                        "model": {"id": "claude-opus-4-5"},
                        "mcp_servers": [],
                        "skills": [],
                        "tools": [],
                    },
                    "archived_at": None,
                    "created_at": "2026-05-08T10:00:00Z",
                    "updated_at": "2026-05-08T10:00:00Z",
                    "environment_id": "env_1",
                    "metadata": {},
                    "resources": [],
                    "stats": {},
                    "status": "idle",
                    "title": None,
                    "usage": {},
                    "vault_ids": [],
                }
            ).model_dump(mode="json"),
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    result = await _get_session_impl(_runtime(client), auth, "ses_1")

    assert isinstance(result, SessionInfo), "should return a SessionInfo"
    assert result.id == "ses_1", "should expose the requested session id"
    assert result.agent_id == "ag_a", "tenant ownership check must pass when agent matches"


async def test_list_session_events_returns_page_envelope() -> None:
    tenant_id = uuid.uuid4()

    user_event = BetaManagedAgentsUserMessageEvent.model_validate(
        {
            "id": "sevt_1",
            "type": "user.message",
            "content": [{"type": "text", "text": "hi"}],
        }
    ).model_dump(mode="json")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=BetaManagedAgentsSession.model_validate(
                {
                    "id": "ses_1",
                    "type": "session",
                    "agent": {
                        "id": "ag_a",
                        "name": "demo",
                        "version": 1,
                        "type": "agent",
                        "model": {"id": "claude-opus-4-5"},
                        "mcp_servers": [],
                        "skills": [],
                        "tools": [],
                    },
                    "archived_at": None,
                    "created_at": "2026-05-08T10:00:00Z",
                    "updated_at": "2026-05-08T10:00:00Z",
                    "environment_id": "env_1",
                    "metadata": {},
                    "resources": [],
                    "stats": {},
                    "status": "idle",
                    "title": None,
                    "usage": {},
                    "vault_ids": [],
                }
            ).model_dump(mode="json"),
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: httpx.Response(200, json={"data": [user_event], "next_page": "p2"}),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    result = await _list_session_events_impl(
        _runtime(client), auth, "ses_1", page=None, limit=None, order=None
    )

    assert len(result.items) == 1, "should expose the one event from the page"
    assert result.next_page == "p2", "should forward SDK next_page cursor unchanged"
    item: SessionEventOut = result.items[0]
    assert item.id == "sevt_1", "event id should round-trip through SDK parse"


async def test_send_message_posts_user_message_with_text_block() -> None:
    tenant_id = uuid.uuid4()

    captured: list[dict[str, Any]] = []

    def on_send(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.append(json_body(req))
        return send_events_response(data=None)

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=BetaManagedAgentsSession.model_validate(
                {
                    "id": "ses_1",
                    "type": "session",
                    "agent": {
                        "id": "ag_a",
                        "name": "demo",
                        "version": 1,
                        "type": "agent",
                        "model": {"id": "claude-opus-4-5"},
                        "mcp_servers": [],
                        "skills": [],
                        "tools": [],
                    },
                    "archived_at": None,
                    "created_at": "2026-05-08T10:00:00Z",
                    "updated_at": "2026-05-08T10:00:00Z",
                    "environment_id": "env_1",
                    "metadata": {},
                    "resources": [],
                    "stats": {},
                    "status": "idle",
                    "title": None,
                    "usage": {},
                    "vault_ids": [],
                }
            ).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/sessions/([^/]+)/events", on_send)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    await _send_message_impl(_runtime(client), auth, "ses_1", "hello")

    assert len(captured) == 1, "should POST to /events exactly once"
    body = captured[0]
    events = body["events"]
    assert events[0]["type"] == "user.message", "event type must be user.message"
    assert events[0]["content"][0]["type"] == "text", "first content block must be text"
    assert events[0]["content"][0]["text"] == "hello", "text payload must round-trip verbatim"


async def test_send_message_raises_when_cross_tenant() -> None:
    tenant_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=BetaManagedAgentsSession.model_validate(
                {
                    "id": "ses_x",
                    "type": "session",
                    "agent": {
                        "id": "ag_other",
                        "name": "other",
                        "version": 1,
                        "type": "agent",
                        "model": {"id": "claude-opus-4-5"},
                        "mcp_servers": [],
                        "skills": [],
                        "tools": [],
                    },
                    "archived_at": None,
                    "created_at": "2026-05-08T10:00:00Z",
                    "updated_at": "2026-05-08T10:00:00Z",
                    "environment_id": "env_1",
                    "metadata": {},
                    "resources": [],
                    "stats": {},
                    "status": "idle",
                    "title": None,
                    "usage": {},
                    "vault_ids": [],
                }
            ).model_dump(mode="json"),
        ),
    )
    # No POST handler — scope check must reject before any send is attempted.
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    with pytest.raises(ToolError, match="session not found"):
        await _send_message_impl(_runtime(client), auth, "ses_x", "hello")
