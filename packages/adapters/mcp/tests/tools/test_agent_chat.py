"""Tests for the agent-chat tool group.

Surface is primitives-only (scoped to the caller's agent): describe_agent,
list_sessions, start_turn, continue_turn, get_session, list_events.

Covers:
1. Narrowing: with a derived-UUID agent_id claim, tools/list returns ONLY the
   agent-chat tools and excludes admin/CRUD tools like list_agents.
2. Round-trip: start_turn returns a handle; get_session reports running→idle;
   list_events exposes the agent.message transcript (primitives-only read).
3. Isolation: a handle whose session agent is not the caller's agent — whether
   cross-tenant or a same-tenant sibling (WR-03) — raises
   ToolError("session not found"); list_sessions is scoped to the caller's agent.
4. Confused-deputy: no tool accepts an agent_id parameter (identity is read
   server-side from the verified claim).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
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
from daimon.adapters.mcp.search_transform import AgentChatAwareBM25SearchTransform
from daimon.adapters.mcp.tools.agent_chat import (
    _continue_turn_impl,
    _describe_agent_impl,
    _get_session_impl,
    _list_events_impl,
    _list_sessions_impl,
    _start_turn_impl,
    register_agent_chat_tools,
)
from daimon.core._models import Tenant  # test-only ORM access escape hatch
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_repo_binding import set_binding
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    MARouter,
    build_fake_anthropic,
    list_response,
    send_events_response,
)
from factories import make_ma_agent
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.transforms import Visibility
from fastmcp.server.transforms.search.base import serialize_tools_for_output_markdown
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Message

pytestmark = pytest.mark.asyncio

_TENANT_ID = uuid.uuid4()
_MA_AGENT_ID = "ag_test001"
_AGENT_UUID = derive_agent_uuid(tenant_id=_TENANT_ID, ma_agent_id=_MA_AGENT_ID)
_ENV_ID = "env_test001"
_ENV_NAME = "production"


def _runtime(
    client: AsyncAnthropic,
    session_factory: Any = None,
    *,
    environment_name: str | None = None,
) -> McpRuntime:
    settings = MagicMock()
    settings.mcp.public_url = None
    settings.mcp.jwt_secret = None
    settings.github.fallback_pat = None
    return McpRuntime(
        session_factory=session_factory or MagicMock(),
        client=client,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(environment_name=environment_name),
    )


def _auth(agent_id: uuid.UUID | None = _AGENT_UUID) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        role=Role.USER,
        agent_id=agent_id,
    )


def _make_fake_session(
    *,
    session_id: str = "ses_test001",
    agent_id: str = _MA_AGENT_ID,
    status: str = "idle",
) -> dict[str, Any]:
    """Build a BetaManagedAgentsSession payload using the real SDK constructor."""
    return BetaManagedAgentsSession.model_validate(
        {
            "id": session_id,
            "type": "session",
            "agent": {
                "id": agent_id,
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
            "environment_id": _ENV_ID,
            "metadata": {},
            "resources": [],
            "stats": {},
            "status": status,
            "title": None,
            "usage": {},
            "vault_ids": [],
        }
    ).model_dump(mode="json")


def _make_idle_event(*, stop_reason_type: str = "end_turn") -> dict[str, Any]:
    """Build a session.status_idle event payload."""
    return {
        "id": "sevt_idle_001",
        "type": "session.status_idle",
        "stop_reason": {
            "type": stop_reason_type,
            "event_ids": [],
        },
    }


def _make_agent_message_event(text: str) -> dict[str, Any]:
    """Build an agent.message event payload with text content."""
    return {
        "id": "sevt_msg_001",
        "type": "agent.message",
        "content": [{"type": "text", "text": text}],
    }


def _make_thread_idle_event(*, stop_reason_type: str = "end_turn") -> dict[str, Any]:
    """Build a ``session.thread_status_idle`` event — the variant the pinned SDK's
    ``BetaManagedAgentsSessionEvent`` union does NOT model, which broke list_events
    output validation on every completed turn in prod.
    """
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
# Helpers
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


async def _tools_list_via_http(app: ASGIApp, token: str) -> list[str]:
    """Initialize an MCP HTTP session and call tools/list; return tool names."""
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
        list_resp = await c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=headers,
        )
        assert list_resp.status_code == 200, f"tools/list failed: {list_resp.text}"
        result = _parse_jsonrpc(list_resp)
    tools_payload = result.get("result", result)
    return [t["name"] for t in tools_payload.get("tools", [])]  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Test 1: Narrowing — agent_id-claim tools/list returns only four agent-chat tools
# ---------------------------------------------------------------------------


async def test_narrowing_agent_id_claim_returns_only_agent_chat_tools() -> None:
    """With an agent_id-claim token, tools/list returns ONLY the four agent-chat tools.

    Verifies that admin/CRUD tools are excluded from the visible set and that only
    describe_agent, start_turn, continue_turn, get_reply are returned.
    This tests the Visibility(False, tags={"agent-chat"}) baseline + narrowing from Plan 02.
    """
    token = "test-agent-token"
    token_claims: dict[str, str] = {
        "sub": str(uuid.uuid4()),
        "tenant_id": str(_TENANT_ID),
        "role": "user",
        "agent_id": str(_AGENT_UUID),
        "client_id": "test",
    }

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)
    mock_sessionmaker: async_sessionmaker[AsyncSession] = MagicMock()  # type: ignore[assignment]

    mcp = FastMCP(
        name="narrowing-test",
        auth=StaticTokenVerifier(tokens={token: token_claims}),
    )
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
    # Baselines: hide admin and agent-chat by default
    mcp.add_transform(Visibility(False, tags={"admin"}))
    mcp.add_transform(Visibility(False, tags={"agent-chat"}))

    runtime = _runtime(client, session_factory=mock_sessionmaker)
    register_agent_chat_tools(mcp, runtime)

    # Add a representative admin tool to verify it remains hidden
    @mcp.tool(tags={"admin"})  # pyright: ignore[reportArgumentType]
    async def list_agents_admin() -> str:  # pyright: ignore[reportUnusedFunction]
        return "admin"

    tool_names = await _tools_list_via_http(mcp.http_app(), token)

    expected = {
        "describe_agent",
        "list_my_sessions",
        "start_turn",
        "continue_turn",
        "get_my_session",
        "list_events",
    }
    assert set(tool_names) == expected, (
        f"agent_id-claim token should see ONLY the agent-chat tools; got: {sorted(tool_names)}"
    )
    assert "list_agents_admin" not in tool_names, (
        "admin tool must not be visible to an agent_id-claim token"
    )


# ---------------------------------------------------------------------------
# Test 1b: Narrowing survives the BM25 search transform (issue #181)
# ---------------------------------------------------------------------------


def _full_stack_mcp(token: str, claims: dict[str, str]) -> FastMCP:
    """Assemble the prod transform stack: both Visibility baselines + the
    agent-chat-aware BM25 search transform + IdentityMiddleware narrowing.

    Mirrors server.py so the test exercises the same listing pipeline that
    returned an empty tools/list in prod (#181). The stock BM25SearchTransform
    collapses the listing to search_tools/call_tool, which the per-agent
    match_all disable then hides — the subclass must yield to the narrowing.
    """
    mock_sessionmaker: async_sessionmaker[AsyncSession] = MagicMock()  # type: ignore[assignment]
    mcp = FastMCP(name="full-stack-181", auth=StaticTokenVerifier(tokens={token: claims}))
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
    mcp.add_transform(Visibility(False, tags={"admin"}))
    mcp.add_transform(Visibility(False, tags={"agent-chat"}))
    mcp.add_transform(
        AgentChatAwareBM25SearchTransform(
            max_results=5,
            always_visible=["list_credentials"],
            search_result_serializer=serialize_tools_for_output_markdown,
        )
    )

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([]))
    runtime = _runtime(build_fake_anthropic(router.dispatch), session_factory=mock_sessionmaker)
    register_agent_chat_tools(mcp, runtime)

    @mcp.tool(tags={"admin"})  # pyright: ignore[reportArgumentType]
    async def list_agents_admin() -> str:  # pyright: ignore[reportUnusedFunction]
        return "admin"

    return mcp


async def test_narrowing_lists_agent_chat_tools_through_bm25_search_transform() -> None:
    """A narrowed agent token's tools/list returns exactly the 6 agent-chat tools
    even with the BM25 search transform in the stack (issue #181).

    The stock BM25SearchTransform collapses the listing to synthetic
    search_tools/call_tool, which the per-agent match_all disable then hides,
    yielding the empty `{"tools": []}` seen in prod. The agent-chat-aware
    subclass yields to the narrowing so the agent-chat tools list directly.
    """
    token = "narrowed-agent-token"
    claims: dict[str, str] = {
        "sub": str(uuid.uuid4()),
        "tenant_id": str(_TENANT_ID),
        "role": "user",
        "agent_id": str(_AGENT_UUID),
        "client_id": "test",
    }

    tool_names = await _tools_list_via_http(_full_stack_mcp(token, claims).http_app(), token)

    expected = {
        "describe_agent",
        "list_my_sessions",
        "start_turn",
        "continue_turn",
        "get_my_session",
        "list_events",
    }
    assert set(tool_names) == expected, (
        "narrowed agent token must list exactly the agent-chat tools through the "
        f"BM25 transform; got: {sorted(tool_names)}"
    )


async def test_non_narrowed_token_still_gets_bm25_search_surface() -> None:
    """A non-narrowed (no agent_id) token still gets the collapsed search surface.

    Guards against the fix over-reaching: only per-agent sessions bypass the
    search collapse; the admin/user surface keeps its search_tools/call_tool
    discovery interface.
    """
    token = "admin-token"
    claims: dict[str, str] = {
        "sub": str(uuid.uuid4()),
        "tenant_id": str(_TENANT_ID),
        "role": "admin",
        "client_id": "test",
    }

    tool_names = await _tools_list_via_http(_full_stack_mcp(token, claims).http_app(), token)

    assert set(tool_names) == {"search_tools", "call_tool"}, (
        "non-narrowed token should get the BM25 search/call surface, not a full "
        f"tool listing; got: {sorted(tool_names)}"
    )


# ---------------------------------------------------------------------------
# Test 2: Start/poll — start_turn creates session, get_reply returns running→done
# ---------------------------------------------------------------------------


async def test_start_turn_then_poll_get_session_and_read_transcript(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """start_turn creates an MA session and returns a handle; get_reply transitions
    running→done with the reduced text from the fake MA events.

    Fake MA flow:
    - GET /v1/agents → the tenant's agent (for _resolve_ma_agent)
    - GET /v1/environments → env list (find_environment_by_daimon_tag)
    - POST /v1/sessions → creates session ses_test001 (status=running)
    - POST /v1/sessions/ses_test001/events → send first user.message
    - GET /v1/sessions/ses_test001 → 1st poll: running; 2nd poll: idle
    - GET /v1/sessions/ses_test001/events → [agent.message, session.status_idle]

    No tenant-scope config row is seeded — the environment_name is resolved
    through ``runtime.deployment_default`` via the real ``resolve()`` cascade
    against Postgres (``db_session_factory``). ``create_session`` is patched
    to avoid its own real-DB writes — the transport-level fake MA handles all
    HTTP interactions.
    """
    call_count: dict[str, int] = {"retrieve": 0}

    def on_session_retrieve(req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        call_count["retrieve"] += 1
        status = "running" if call_count["retrieve"] == 1 else "idle"
        return httpx.Response(200, json=_make_fake_session(status=status))

    env_payload = {
        "id": _ENV_ID,
        "type": "environment",
        "name": _ENV_NAME,
        "config": EMPTY_CLOUD_CONFIG.model_dump(mode="json"),
        "description": "",
        "metadata": {
            "daimon_tenant": str(_TENANT_ID),
            "daimon_name": _ENV_NAME,
        },
        "created_at": "2026-06-23T00:00:00Z",
        "updated_at": "2026-06-23T00:00:00Z",
    }

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id=_MA_AGENT_ID,
                    name="test-agent",
                    metadata={
                        "daimon_tenant": str(_TENANT_ID),
                        "daimon_name": "test-agent",
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/environments",
        lambda _r, _m: list_response([env_payload]),
    )
    router.add("GET", r"/v1/sessions/([^/]+)", on_session_retrieve)
    router.add(
        "POST",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: send_events_response(data=None),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: httpx.Response(
            200,
            json={
                "data": [
                    _make_agent_message_event("Hello from agent"),
                    _make_idle_event(stop_reason_type="end_turn"),
                ],
                "next_page": None,
            },
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    auth = _auth()

    # Build a fake session returned by the patched create_session
    fake_session = BetaManagedAgentsSession.model_validate(_make_fake_session(status="running"))

    with patch(
        "daimon.adapters.mcp.tools.agent_chat.create_session",
        new=AsyncMock(return_value=fake_session),
    ):
        start_result = await _start_turn_impl(runtime, auth, "Say hello")

    assert "handle" in start_result, "start_turn should return a handle dict"
    handle: str = start_result["handle"]
    assert handle == "ses_test001", "handle should be the MA session id"

    # First poll via get_session: session is running.
    running = await _get_session_impl(runtime, auth, handle)
    assert running.status == "running", "first get_session should report running"

    # Second poll: session is idle.
    done = await _get_session_impl(runtime, auth, handle)
    assert done.status == "idle", "second get_session should report idle once finished"

    # Read the reply from the transcript (primitives-only: caller folds events).
    events_page = await _list_events_impl(runtime, auth, handle, None, None, "asc")
    texts = [
        block["text"]
        for ev in events_page.items
        if ev.type == "agent.message"
        for block in (ev.content or [])
        if block.get("type") == "text"
    ]
    assert "Hello from agent" in texts, (
        f"agent.message text should be readable from list_events; got {texts!r}"
    )


# ---------------------------------------------------------------------------
# Test 2b: MPP-01 regression — env resolves from deployment_default alone
# ---------------------------------------------------------------------------


async def test_start_turn_resolves_env_from_deployment_default_when_no_tenant_row(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant with NO tenant-scope config row still resolves environment_name
    from ``runtime.deployment_default`` and creates a session (MPP-01).

    Regression for the bug where ``_fetch_tenant_environment_name`` read only
    the tenant-scope row and returned None for every tenant relying on the
    deployment default, raising ``ToolError("environment not found")`` even
    though the shared ``resolve()`` cascade (used by Discord) would have
    found the environment via the bottom (deployment) tier.
    """
    env_payload = {
        "id": _ENV_ID,
        "type": "environment",
        "name": _ENV_NAME,
        "config": EMPTY_CLOUD_CONFIG.model_dump(mode="json"),
        "description": "",
        "metadata": {
            "daimon_tenant": str(_TENANT_ID),
            "daimon_name": _ENV_NAME,
        },
        "created_at": "2026-06-23T00:00:00Z",
        "updated_at": "2026-06-23T00:00:00Z",
    }

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id=_MA_AGENT_ID,
                    name="test-agent",
                    metadata={
                        "daimon_tenant": str(_TENANT_ID),
                        "daimon_name": "test-agent",
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/environments",
        lambda _r, _m: list_response([env_payload]),
    )
    router.add(
        "POST",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: send_events_response(data=None),
    )
    client = build_fake_anthropic(router.dispatch)
    # No tenant-scope config row is ever written to db_session_factory's schema
    # — resolve() must fall through channel(None) -> tenant(None) -> deployment.
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    auth = _auth()

    fake_session = BetaManagedAgentsSession.model_validate(_make_fake_session(status="running"))
    with patch(
        "daimon.adapters.mcp.tools.agent_chat.create_session",
        new=AsyncMock(return_value=fake_session),
    ):
        start_result = await _start_turn_impl(runtime, auth, "Say hello")

    assert start_result["handle"] == "ses_test001", (
        "start_turn should resolve the deployment-default environment and create a "
        "session instead of raising 'environment not found'"
    )


# ---------------------------------------------------------------------------
# Test 3: Cross-tenant — handle from another tenant raises ToolError
# ---------------------------------------------------------------------------


async def test_get_session_raises_session_not_found_for_cross_tenant_handle() -> None:
    """A handle whose session agent is not in the caller's tenant raises ToolError.

    The error message is identical for unknown vs. forbidden — no existence leak
    across tenant boundaries (Tampering threat mitigation).
    """
    other_agent_id = "ag_other_tenant"

    router = MARouter()
    # Tenant only has _MA_AGENT_ID, NOT ag_other_tenant
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id=_MA_AGENT_ID,
                    name="test-agent",
                    metadata={
                        "daimon_tenant": str(_TENANT_ID),
                        "daimon_name": "test-agent",
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    # The session belongs to ag_other_tenant — cross-tenant handle
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=_make_fake_session(
                session_id="ses_cross",
                agent_id=other_agent_id,
                status="idle",
            ),
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    with pytest.raises(ToolError, match="session not found"):
        await _get_session_impl(runtime, auth, "ses_cross")


# ---------------------------------------------------------------------------
# Test 3b: Same-tenant cross-agent — handle for a SIBLING agent raises ToolError
# (WR-03: agent-ownership, not just tenant-ownership)
# ---------------------------------------------------------------------------


def _sibling_tenant_agents_router() -> MARouter:
    """Router whose tenant owns TWO agents: the caller and a sibling.

    Tenant-ownership alone would pass for either agent's session. The
    agent-ownership check must still reject the sibling's session.
    """
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id=_MA_AGENT_ID,
                    name="test-agent",
                    metadata={
                        "daimon_tenant": str(_TENANT_ID),
                        "daimon_name": "test-agent",
                    },
                ).model_dump(mode="json"),
                make_ma_agent(
                    id="ag_sibling",
                    name="sibling-agent",
                    metadata={
                        "daimon_tenant": str(_TENANT_ID),
                        "daimon_name": "sibling-agent",
                    },
                ).model_dump(mode="json"),
            ]
        ),
    )
    return router


async def test_get_session_raises_session_not_found_for_same_tenant_other_agent_handle() -> None:
    """A handle whose session belongs to a SIBLING agent in the same tenant raises.

    Tenant-ownership passes (both agents share the tenant), so this proves the
    check is agent-scoped: the caller (test-agent) must not poll ag_sibling's
    session even though they share a tenant (WR-03).
    """
    router = _sibling_tenant_agents_router()
    # The session belongs to ag_sibling — same tenant, different agent.
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=_make_fake_session(
                session_id="ses_sibling",
                agent_id="ag_sibling",
                status="running",
            ),
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    with pytest.raises(ToolError, match="session not found"):
        await _get_session_impl(runtime, auth, "ses_sibling")


async def test_continue_turn_raises_session_not_found_for_same_tenant_other_agent_handle() -> None:
    """continue_turn must reject a sibling agent's session even within the tenant.

    Without agent-ownership, the caller could inject a message into another
    agent's session by guessing its handle (WR-03).
    """
    router = _sibling_tenant_agents_router()
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=_make_fake_session(
                session_id="ses_sibling",
                agent_id="ag_sibling",
                status="idle",
            ),
        ),
    )
    # A send route exists so that if the guard wrongly passes, the failure is a
    # missing ToolError (clean RED), not an unrelated transport 404.
    router.add(
        "POST",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: send_events_response(data=None),
    )
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    with pytest.raises(ToolError, match="session not found"):
        await _continue_turn_impl(runtime, auth, "ses_sibling", "hi")


# ---------------------------------------------------------------------------
# Test 4: Confused-deputy — no tool accepts an agent_id parameter
# ---------------------------------------------------------------------------


async def test_agent_chat_tools_have_no_agent_id_parameter() -> None:
    """None of the agent-chat tools accept an agent_id parameter.

    Agent identity is read server-side from auth.agent_id (the verified JWT claim);
    accepting agent_id as a tool argument would be a confused-deputy vulnerability.
    """
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    mcp = FastMCP(name="test")
    runtime = _runtime(client)
    register_agent_chat_tools(mcp, runtime)

    agent_chat_names = {
        "describe_agent",
        "list_my_sessions",
        "start_turn",
        "continue_turn",
        "get_my_session",
        "list_events",
    }

    for tool_name in agent_chat_names:
        tool = await mcp.get_tool(tool_name)
        assert tool is not None, f"Tool '{tool_name}' should be registered"
        schema: dict[str, Any] = tool.parameters or {}
        properties: dict[str, Any] = schema.get("properties", {})
        assert "agent_id" not in properties, (
            f"Tool '{tool_name}' must not accept 'agent_id' as a parameter — "
            "agent identity is read server-side from the verified claim "
            "(confused-deputy mitigation)"
        )


# ---------------------------------------------------------------------------
# Test 5: list_sessions is agent-scoped — lists only the caller's agent's sessions
# ---------------------------------------------------------------------------


async def test_list_sessions_lists_only_the_callers_agent_sessions() -> None:
    """list_sessions resolves the caller's agent and lists ONLY that agent's
    sessions — it passes the caller's MA agent id to sessions.list, never a
    tenant-wide drain."""
    seen_agent_ids: list[str] = []

    def on_sessions_list(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        seen_agent_ids.append(req.url.params.get("agent_id", ""))
        return list_response(
            [
                _make_fake_session(session_id="ses_a", agent_id=_MA_AGENT_ID, status="idle"),
                _make_fake_session(session_id="ses_b", agent_id=_MA_AGENT_ID, status="running"),
            ]
        )

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id=_MA_AGENT_ID,
                    name="test-agent",
                    metadata={
                        "daimon_tenant": str(_TENANT_ID),
                        "daimon_name": "test-agent",
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/sessions", on_sessions_list)
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    sessions = await _list_sessions_impl(runtime, auth)

    assert {s.id for s in sessions} == {"ses_a", "ses_b"}, (
        f"should return the caller's agent's sessions; got {[s.id for s in sessions]!r}"
    )
    assert seen_agent_ids == [_MA_AGENT_ID], (
        f"list must be scoped to the caller's MA agent id; got query agent_ids {seen_agent_ids!r}"
    )


# ---------------------------------------------------------------------------
# Test 6: MPP-03 — describe_agent reports the real bound repo URL
# ---------------------------------------------------------------------------


def _describe_agent_router() -> MARouter:
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id=_MA_AGENT_ID,
                    name="test-agent",
                    metadata={
                        "daimon_tenant": str(_TENANT_ID),
                        "daimon_name": "test-agent",
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    return router


async def test_describe_agent_returns_bound_repo_url_for_bound_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A bound agent's describe_agent().repo_url equals the (normalized) bound URL."""
    client = build_fake_anthropic(_describe_agent_router().dispatch)
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    auth = _auth()

    async with db_session_factory() as session, session.begin():
        session.add(Tenant(id=_TENANT_ID, platform="discord", external_id=str(_TENANT_ID)))
        await session.flush()
        await set_binding(
            session,
            tenant_id=_TENANT_ID,
            agent_id=_AGENT_UUID,
            repo_url="https://github.com/acme/widgets",
            default_branch="main",
            ma_secret_ref="anon:",
        )

    description = await _describe_agent_impl(runtime, auth)

    assert description.repo_url == "acme/widgets", (
        f"describe_agent should report the bound (normalized) repo URL; got {description.repo_url!r}"
    )


async def test_describe_agent_returns_none_repo_url_for_unbound_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unbound agent's describe_agent().repo_url is None (no binding row seeded)."""
    client = build_fake_anthropic(_describe_agent_router().dispatch)
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    auth = _auth()

    description = await _describe_agent_impl(runtime, auth)

    assert description.repo_url is None, (
        f"describe_agent should report None for a genuinely unbound agent; got {description.repo_url!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: list_events output schema admits session.thread_status_* events
# ---------------------------------------------------------------------------


async def _call_tool_via_http(
    app: ASGIApp, token: str, name: str, arguments: dict[str, object]
) -> dict[str, object]:
    """Initialize an MCP HTTP session and call tools/call; return the JSON-RPC result.

    Goes through the full server pipeline (auth -> IdentityMiddleware -> tool ->
    FastMCP OUTPUT VALIDATION) so it exercises the same output-schema check that
    rejected the transcript in prod — unlike the _impl-level tests which bypass it.
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


async def test_list_events_admits_thread_status_events_through_fastmcp() -> None:
    """list_events must return a transcript containing session.thread_status_idle
    (a variant the pinned SDK's event union does NOT model) without a FastMCP
    output-validation error.

    Regression for the prod failure: every completed turn emits
    session.thread_status_running/idle, and pinning the tool's OUTPUT schema to
    BetaManagedAgentsSessionEvent made list_events return isError on the whole
    transcript. Exercised through the HTTP pipeline so the output-schema check
    actually runs (the _impl-level transcript test bypasses it).
    """
    token = "narrowed-agent-token"
    claims: dict[str, str] = {
        "sub": str(uuid.uuid4()),
        "tenant_id": str(_TENANT_ID),
        "role": "user",
        "agent_id": str(_AGENT_UUID),
        "client_id": "test",
    }

    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: httpx.Response(
            200,
            json={
                "data": [
                    _make_agent_message_event("Hello from agent"),
                    _make_thread_idle_event(stop_reason_type="end_turn"),
                ],
                "next_page": None,
            },
        ),
    )
    # _verify_agent_owns_session retrieves the session and derives its agent UUID.
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(200, json=_make_fake_session(status="idle")),
    )

    mock_sessionmaker: async_sessionmaker[AsyncSession] = MagicMock()  # type: ignore[assignment]
    mcp = FastMCP(name="list-events-schema", auth=StaticTokenVerifier(tokens={token: claims}))
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
    mcp.add_transform(Visibility(False, tags={"agent-chat"}))
    runtime = _runtime(build_fake_anthropic(router.dispatch), session_factory=mock_sessionmaker)
    register_agent_chat_tools(mcp, runtime)

    result = await _call_tool_via_http(
        mcp.http_app(), token, "list_events", {"handle": "ses_test001"}
    )

    payload = result.get("result", result)
    assert isinstance(payload, dict), f"unexpected tools/call shape: {result!r}"
    assert not payload.get("isError"), (
        f"list_events must not output-validation-error on a thread_status_idle event; got {payload!r}"
    )
    structured = payload.get("structuredContent") or {}
    types = [ev.get("type") for ev in structured.get("items", [])]  # type: ignore[union-attr]
    assert "session.thread_status_idle" in types, (
        f"the thread_status_idle event must survive in the transcript; got types {types!r}"
    )
    assert "agent.message" in types, "the agent.message reply must still be present"
