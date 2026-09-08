"""Tests for the scoped agent-chat primitives and bounded ``ask`` tool.

Covers:
1. Narrowing: with a derived-UUID agent_id claim, tools/list returns ONLY the
   agent-chat tools and excludes admin/CRUD tools like list_agents.
2. Round-trip: start_turn returns a handle; get_session reports running→idle;
   list_events exposes the transcript, while ask folds the same flow.
3. Isolation: a handle whose session agent is not the caller's agent — whether
   cross-tenant or a same-tenant sibling (WR-03) — raises
   ToolError("session not found"); list_sessions is scoped to the caller's agent.
4. Confused-deputy: no tool accepts an agent_id parameter (identity is read
   server-side from the verified claim).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import re
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession, FileMetadata
from anthropic.types.beta.sessions import (
    BetaManagedAgentsSpanModelRequestEndEvent,
    BetaManagedAgentsSpanModelUsage,
    BetaManagedAgentsTextBlock,
    BetaManagedAgentsUserMessageEvent,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.hosted_artifacts import ChartUrl, HostedChartDelivery
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
    AskResult,
    _archive_my_session_impl,
    _ask_impl,
    _ask_tool_result,
    _cancel_turn_impl,
    _continue_turn_impl,
    _deliver_turn_charts_impl,
    _describe_agent_impl,
    _get_session_impl,
    _get_turn_cost_impl,
    _list_events_impl,
    _list_sessions_impl,
    _start_turn_impl,
    register_agent_chat_tools,
)
from daimon.adapters.mcp.tools.sessions import SessionEventOut
from daimon.core import bundle_handle
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_repo_binding import set_binding
from daimon.core.tenant_balance import debit_amount
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
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
from fastmcp.server.transforms import Visibility
from fastmcp.server.transforms.search.base import serialize_tools_for_output_markdown
from fastmcp.tools import ToolResult
from mcp.types import ImageContent
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Message

pytestmark = pytest.mark.asyncio

_BUNDLE_SECRET = "test-bundle-handle-secret"
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


def _timed_event(
    event_id: str,
    event_type: str,
    processed_at: dt.datetime,
    *,
    text: str | None = None,
) -> SessionEventOut:
    content = [{"type": "text", "text": text}] if text is not None else []
    return SessionEventOut(
        id=event_id,
        type=event_type,
        content=content,
        processed_at=processed_at.isoformat().replace("+00:00", "Z"),
    )


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
# Test 1: Narrowing — agent_id-claim tools/list returns only agent-chat tools
# ---------------------------------------------------------------------------


async def test_narrowing_agent_id_claim_returns_only_agent_chat_tools() -> None:
    """With an agent_id-claim token, tools/list returns only agent-chat tools.

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
    register_agent_chat_tools(mcp, runtime, billing_config=None)

    # Add a representative admin tool to verify it remains hidden
    @mcp.tool(tags={"admin"})  # pyright: ignore[reportArgumentType]
    async def list_agents_admin() -> str:  # pyright: ignore[reportUnusedFunction]
        return "admin"

    tool_names = await _tools_list_via_http(mcp.http_app(), token)

    expected = {
        "ask",
        "describe_agent",
        "list_my_sessions",
        "start_turn",
        "continue_turn",
        "deliver_turn_charts",
        "get_my_session",
        "list_events",
        "archive_my_session",
        "cancel_turn",
        "get_turn_cost",
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
    register_agent_chat_tools(mcp, runtime, billing_config=None)

    @mcp.tool(tags={"admin"})  # pyright: ignore[reportArgumentType]
    async def list_agents_admin() -> str:  # pyright: ignore[reportUnusedFunction]
        return "admin"

    return mcp


async def test_narrowing_lists_agent_chat_tools_through_bm25_search_transform() -> None:
    """A narrowed agent token's tools/list returns exactly the agent-chat tools
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
        "ask",
        "describe_agent",
        "list_my_sessions",
        "start_turn",
        "continue_turn",
        "deliver_turn_charts",
        "get_my_session",
        "list_events",
        "archive_my_session",
        "cancel_turn",
        "get_turn_cost",
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
        lambda _r, _m: send_events_response(
            data=[
                BetaManagedAgentsUserMessageEvent(
                    id="sevt_start_boundary",
                    content=[BetaManagedAgentsTextBlock(type="text", text="Say hello")],
                    type="user.message",
                    processed_at=dt.datetime(2026, 6, 23, 0, 0, tzinfo=dt.UTC),
                ).model_dump(mode="json")
            ]
        ),
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


async def test_ask_delivers_embedded_charts_without_artifact_settings() -> None:
    runtime = MagicMock()
    runtime.settings.artifacts = None
    runtime.artifact_store = None
    auth = _auth()
    running = MagicMock(status="running")
    idle = MagicMock(status="idle")
    events = MagicMock(
        items=[
            MagicMock(
                type="agent.message",
                content=[{"type": "text", "text": "Final answer"}],
            )
        ],
        next_page=None,
    )
    image = ImageContent(type="image", data="cG5n", mimeType="image/png")
    turn_started_at = dt.datetime(2026, 8, 25, 12, 0, tzinfo=dt.UTC)

    with (
        patch(
            "daimon.adapters.mcp.tools.agent_chat._start_turn_impl",
            new=AsyncMock(return_value={"handle": "ses_ask001"}),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat._get_session_impl",
            new=AsyncMock(side_effect=[running, idle]),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat._list_events_impl",
            new=AsyncMock(return_value=events),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat.deliver_hosted_charts",
            new=AsyncMock(
                return_value=HostedChartDelivery(
                    message="Final answer",
                    image_blocks=(image,),
                )
            ),
        ) as deliver,
    ):
        result = await _ask_impl(
            runtime,
            auth,
            "Question",
            sleep=AsyncMock(),
            clock=lambda: 0.0,
            now=lambda: turn_started_at,
        )

    assert result == AskResult(
        handle="ses_ask001",
        message="Final answer",
        image_blocks=(image,),
    )
    deliver.assert_awaited_once_with(
        runtime.client,
        settings=None,
        tenant_id=str(auth.tenant_id),
        account_id=str(auth.account_id),
        session_id="ses_ask001",
        turn_started_at=turn_started_at,
        message="Final answer",
        store=None,
    )


async def test_ask_timeout_preserves_the_resumable_handle() -> None:
    runtime = MagicMock()
    runtime.settings.artifacts = None
    runtime.artifact_store = None
    auth = _auth()
    elapsed = 0.0

    async def advance(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    with (
        patch(
            "daimon.adapters.mcp.tools.agent_chat._start_turn_impl",
            new=AsyncMock(return_value={"handle": "ses_slow001"}),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat._get_session_impl",
            new=AsyncMock(return_value=MagicMock(status="running")),
        ) as get_session,
        patch(
            "daimon.adapters.mcp.tools.agent_chat._list_events_impl",
            new=AsyncMock(),
        ) as list_events,
        pytest.raises(ToolError, match=r"120 seconds.*ses_slow001"),
    ):
        await _ask_impl(
            runtime,
            auth,
            "Slow question",
            timeout_seconds=120.0,
            poll_interval_seconds=60.0,
            clock=lambda: elapsed,
            sleep=advance,
        )

    assert get_session.await_count == 3
    list_events.assert_not_awaited()


async def test_ask_surfaces_terminal_non_idle_status_without_waiting() -> None:
    runtime = MagicMock()
    auth = _auth()
    sleep = AsyncMock()

    with (
        patch(
            "daimon.adapters.mcp.tools.agent_chat._start_turn_impl",
            new=AsyncMock(return_value={"handle": "ses_terminated001"}),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat._get_session_impl",
            new=AsyncMock(return_value=MagicMock(status="terminated")),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat._list_events_impl",
            new=AsyncMock(),
        ) as list_events,
        pytest.raises(ToolError, match=r"terminal status 'terminated'.*ses_terminated001"),
    ):
        await _ask_impl(runtime, auth, "Question", sleep=sleep)

    sleep.assert_not_awaited()
    list_events.assert_not_awaited()


async def test_ask_tool_result_preserves_images_and_structured_urls() -> None:
    image = ImageContent(type="image", data="cG5n", mimeType="image/png")
    chart = ChartUrl(
        filename="chart.png",
        url="https://bucket.example.test/chart.png?signed=yes",
        expires_at=dt.datetime(2026, 8, 25, 12, 10, tzinfo=dt.UTC),
    )

    result = _ask_tool_result(
        AskResult(
            handle="ses_ask001",
            message="Answer with chart",
            chart_urls=(chart,),
            image_blocks=(image,),
        )
    )

    assert isinstance(result, ToolResult)
    assert result.content[0].model_dump(by_alias=True, exclude_none=True) == {
        "type": "text",
        "text": "Answer with chart",
    }
    assert result.content[1].model_dump(by_alias=True, exclude_none=True) == {
        "type": "image",
        "data": "cG5n",
        "mimeType": "image/png",
    }
    assert result.structured_content is not None
    assert result.structured_content["chart_urls"][0]["filename"] == "chart.png"
    assert "image_blocks" not in result.structured_content

    embed_only = _ask_tool_result(
        AskResult(
            handle="ses_ask002",
            message="Answer with embedded chart",
            image_blocks=(image,),
        )
    )
    assert isinstance(embed_only, ToolResult)
    assert embed_only.structured_content is not None
    assert embed_only.structured_content["chart_urls"] == []
    assert embed_only.content[1] == image


async def test_ask_result_schema_omits_the_unserialized_image_blocks() -> None:
    schema = AskResult.model_json_schema()

    assert "image_blocks" not in schema["properties"], (
        "image_blocks is excluded from every dump, so advertising it in the "
        "output schema points clients at a field that never arrives"
    )


async def test_ask_tool_result_keeps_prose_shape_without_charts() -> None:
    result = _ask_tool_result(AskResult(handle="ses_plain001", message="Plain answer"))

    assert isinstance(result, ToolResult)
    assert result.content[0].model_dump(by_alias=True, exclude_none=True) == {
        "type": "text",
        "text": "Plain answer",
    }
    assert result.structured_content is not None
    assert result.structured_content["handle"] == "ses_plain001"


async def test_ask_keeps_polling_through_an_unmodeled_transient_status() -> None:
    runtime = MagicMock()
    runtime.settings.artifacts = None
    runtime.artifact_store = None
    auth = _auth()
    events = MagicMock(
        items=[
            MagicMock(
                type="agent.message",
                content=[{"type": "text", "text": "Final answer"}],
            )
        ],
        next_page=None,
    )

    with (
        patch(
            "daimon.adapters.mcp.tools.agent_chat._start_turn_impl",
            new=AsyncMock(return_value={"handle": "ses_queued001"}),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat._get_session_impl",
            new=AsyncMock(side_effect=[MagicMock(status="queued"), MagicMock(status="idle")]),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat._list_events_impl",
            new=AsyncMock(return_value=events),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat.deliver_hosted_charts",
            new=AsyncMock(return_value=HostedChartDelivery(message="Final answer")),
        ),
    ):
        result = await _ask_impl(
            runtime,
            auth,
            "Question",
            sleep=AsyncMock(),
            clock=lambda: 0.0,
        )

    assert result.message == "Final answer"


async def test_deliver_turn_charts_bounds_the_transcript_walk() -> None:
    runtime = MagicMock()
    auth = _auth()
    reply_at = dt.datetime(2026, 8, 25, 12, 20, tzinfo=dt.UTC)

    def endless_page(*args: Any, **kwargs: Any) -> MagicMock:
        del args, kwargs
        return MagicMock(
            items=[_timed_event("sevt_reply", "agent.message", reply_at, text="Answer")],
            next_page="more",
        )

    with (
        patch(
            "daimon.adapters.mcp.tools.agent_chat._get_session_impl",
            new=AsyncMock(
                return_value=MagicMock(
                    status="idle",
                    created_at=dt.datetime(2026, 8, 25, 12, 0, tzinfo=dt.UTC),
                )
            ),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat._list_events_impl",
            new=AsyncMock(side_effect=endless_page),
        ) as list_events,
        patch("daimon.adapters.mcp.tools.agent_chat._MAX_EVENT_PAGES", 2),
        pytest.raises(ToolError, match="no completed turn boundary"),
    ):
        await _deliver_turn_charts_impl(runtime, auth, "ses_endless001")

    assert list_events.await_count == 2


async def test_deliver_turn_charts_serves_repeat_calls_from_cache() -> None:
    runtime = MagicMock()
    runtime.settings.artifacts = None
    runtime.artifact_store = None
    auth = _auth()
    reply_at = dt.datetime(2026, 8, 25, 12, 20, tzinfo=dt.UTC)
    turn_started_at = dt.datetime(2026, 8, 25, 12, 10, tzinfo=dt.UTC)

    def transcript(*args: Any, **kwargs: Any) -> MagicMock:
        del args, kwargs
        return MagicMock(
            items=[
                _timed_event("sevt_reply", "agent.message", reply_at, text="Final answer"),
                _timed_event("sevt_turn_start", "user.message", turn_started_at),
            ],
            next_page=None,
        )

    with (
        patch(
            "daimon.adapters.mcp.tools.agent_chat._get_session_impl",
            new=AsyncMock(
                return_value=MagicMock(
                    status="idle",
                    created_at=dt.datetime(2026, 8, 25, 12, 0, tzinfo=dt.UTC),
                )
            ),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat._list_events_impl",
            new=AsyncMock(side_effect=transcript),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat.deliver_hosted_charts",
            new=AsyncMock(return_value=HostedChartDelivery(message="Final answer")),
        ) as deliver,
    ):
        first = await _deliver_turn_charts_impl(runtime, auth, "ses_retry001")
        second = await _deliver_turn_charts_impl(runtime, auth, "ses_retry001")

    assert first == second
    deliver.assert_awaited_once()


async def test_deliver_turn_charts_uses_newest_completed_turn_window() -> None:
    runtime = MagicMock()
    runtime.settings.artifacts = None
    runtime.artifact_store = None
    auth = _auth()
    newer_user_at = dt.datetime(2026, 8, 25, 12, 30, tzinfo=dt.UTC)
    reply_at = dt.datetime(2026, 8, 25, 12, 20, tzinfo=dt.UTC)
    turn_started_at = dt.datetime(2026, 8, 25, 12, 10, tzinfo=dt.UTC)
    pages = [
        MagicMock(
            items=[
                _timed_event("sevt_next_turn", "user.message", newer_user_at),
                _timed_event(
                    "sevt_newest_reply",
                    "agent.message",
                    reply_at,
                    text="Final answer",
                ),
            ],
            next_page="older-events",
        ),
        MagicMock(
            items=[_timed_event("sevt_turn_start", "user.message", turn_started_at)],
            next_page=None,
        ),
    ]
    image = ImageContent(type="image", data="cG5n", mimeType="image/png")

    with (
        patch(
            "daimon.adapters.mcp.tools.agent_chat._get_session_impl",
            new=AsyncMock(
                return_value=MagicMock(
                    status="idle",
                    created_at=dt.datetime(2026, 8, 25, 12, 0, tzinfo=dt.UTC),
                )
            ),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat._list_events_impl",
            new=AsyncMock(side_effect=pages),
        ) as list_events,
        patch(
            "daimon.adapters.mcp.tools.agent_chat.deliver_hosted_charts",
            new=AsyncMock(
                return_value=HostedChartDelivery(
                    message="Final answer",
                    image_blocks=(image,),
                )
            ),
        ) as deliver,
    ):
        result = await _deliver_turn_charts_impl(runtime, auth, "ses_chart_turn")

    assert result == AskResult(
        handle="ses_chart_turn",
        message="Final answer",
        image_blocks=(image,),
    )
    assert list_events.await_args_list == [
        ((runtime, auth, "ses_chart_turn", None, 100, "desc"), {}),
        ((runtime, auth, "ses_chart_turn", "older-events", 100, "desc"), {}),
    ]
    deliver.assert_awaited_once_with(
        runtime.client,
        settings=None,
        tenant_id=str(auth.tenant_id),
        account_id=str(auth.account_id),
        session_id="ses_chart_turn",
        turn_started_at=turn_started_at,
        message="Final answer",
        store=None,
    )


async def test_deliver_turn_charts_rejects_session_without_completed_reply() -> None:
    runtime = MagicMock()
    auth = _auth()
    events = MagicMock(
        items=[
            _timed_event(
                "sevt_user_only",
                "user.message",
                dt.datetime(2026, 8, 25, 12, 10, tzinfo=dt.UTC),
            )
        ],
        next_page=None,
    )

    with (
        patch(
            "daimon.adapters.mcp.tools.agent_chat._get_session_impl",
            new=AsyncMock(
                return_value=MagicMock(
                    status="idle",
                    created_at=dt.datetime(2026, 8, 25, 12, 0, tzinfo=dt.UTC),
                )
            ),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat._list_events_impl",
            new=AsyncMock(return_value=events),
        ),
        pytest.raises(ToolError, match="no completed turn"),
    ):
        await _deliver_turn_charts_impl(runtime, auth, "ses_no_reply")


async def test_deliver_turn_charts_refuses_before_session_is_complete() -> None:
    runtime = MagicMock()
    auth = _auth()

    with (
        patch(
            "daimon.adapters.mcp.tools.agent_chat._get_session_impl",
            new=AsyncMock(
                return_value=MagicMock(
                    status="running",
                    created_at=dt.datetime(2026, 8, 25, 12, 0, tzinfo=dt.UTC),
                )
            ),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat._list_events_impl",
            new=AsyncMock(),
        ) as list_events,
        pytest.raises(ToolError, match=r"'running'.*idle.*terminated"),
    ):
        await _deliver_turn_charts_impl(runtime, auth, "ses_running")

    list_events.assert_not_awaited()


async def test_deliver_turn_charts_falls_back_when_boundary_has_no_timestamp() -> None:
    runtime = MagicMock()
    runtime.settings.artifacts = None
    runtime.artifact_store = None
    auth = _auth()
    session_created_at = dt.datetime(2026, 8, 25, 11, 0, tzinfo=dt.UTC)
    events = MagicMock(
        items=[
            _timed_event(
                "sevt_reply",
                "agent.message",
                dt.datetime(2026, 8, 25, 12, 20, tzinfo=dt.UTC),
                text="Final answer",
            ),
            SessionEventOut(id="sevt_boundary", type="user.message", content=[]),
        ],
        next_page=None,
    )

    with (
        patch(
            "daimon.adapters.mcp.tools.agent_chat._get_session_impl",
            new=AsyncMock(
                return_value=MagicMock(status="terminated", created_at=session_created_at)
            ),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat._list_events_impl",
            new=AsyncMock(return_value=events),
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat.deliver_hosted_charts",
            new=AsyncMock(return_value=HostedChartDelivery(message="Final answer")),
        ) as deliver,
    ):
        result = await _deliver_turn_charts_impl(runtime, auth, "ses_no_boundary_time")

    assert result.message == "Final answer"
    assert deliver.await_args.kwargs["turn_started_at"] == session_created_at


async def test_deliver_turn_charts_is_not_annotated_read_only() -> None:
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([]))
    mcp = FastMCP(name="agent-chat-annotations")
    register_agent_chat_tools(
        mcp,
        _runtime(build_fake_anthropic(router.dispatch)),
        billing_config=None,
    )

    tool = await mcp.get_tool("deliver_turn_charts")

    assert tool is not None
    assert tool.annotations is None or tool.annotations.readOnlyHint is not True


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
        lambda _r, _m: send_events_response(
            data=[
                BetaManagedAgentsUserMessageEvent(
                    id="sevt_start_boundary",
                    content=[BetaManagedAgentsTextBlock(type="text", text="Say hello")],
                    type="user.message",
                    processed_at=dt.datetime(2026, 6, 23, 0, 0, tzinfo=dt.UTC),
                ).model_dump(mode="json")
            ]
        ),
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
    register_agent_chat_tools(mcp, runtime, billing_config=None)

    agent_chat_names = {
        "ask",
        "describe_agent",
        "list_my_sessions",
        "start_turn",
        "continue_turn",
        "deliver_turn_charts",
        "get_my_session",
        "list_events",
        "archive_my_session",
        "cancel_turn",
        "get_turn_cost",
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
        await make_tenant(session, platform="discord", workspace_id=str(_TENANT_ID), id=_TENANT_ID)
        await set_binding(
            session,
            tenant_id=_TENANT_ID,
            agent_id=_AGENT_UUID,
            repo_url="https://github.com/acme/widgets",
            default_branch="main",
            ma_secret_ref="anon:",
            proof=None,
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
    register_agent_chat_tools(mcp, runtime, billing_config=None)

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


# ---------------------------------------------------------------------------
# Test 5: admission — turn-creating tools refuse before touching MA
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ["start_turn", "ask"])
async def test_turn_tools_refuse_over_balance_tenant_before_creating_session(
    db_session_factory: async_sessionmaker[AsyncSession],
    tool_name: str,
) -> None:
    """Turn-creating tools refuse a tenant with no credit before touching MA.

    Drives the tool through the real closure (tools/call over HTTP, not the
    impl) with a claims token that carries platform_user_id so the gate's
    unbilled internal-token path is not taken. The tenant is seeded with no
    ledger entries, so its balance is 0 and is_over_balance is True.
    """
    tenant_id = uuid.uuid4()
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(
            session, platform="discord", workspace_id=str(tenant_id), id=tenant_id
        )
        account = await make_account(session, tenant=tenant)

    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_MA_AGENT_ID)
    token = "over-balance-agent-token"
    claims: dict[str, str] = {
        "sub": str(account.id),
        "tenant_id": str(tenant_id),
        "role": "user",
        "agent_id": str(agent_uuid),
        "platform_user_id": "discord-user-over-balance",
        "client_id": "test",
    }

    router = MARouter()
    mcp = FastMCP(
        name="admission-over-balance",
        auth=StaticTokenVerifier(tokens={token: claims}),
    )
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=production_subject_resolver,
            tenant_resolver=production_tenant_resolver,
            role_resolver=production_role_resolver,
            agent_id_resolver=production_agent_id_resolver,
            is_admin_resolver=production_is_admin_resolver,
            internal_resolver=production_internal_resolver,
            sessionmaker=db_session_factory,
        )
    )
    mcp.add_transform(Visibility(False, tags={"agent-chat"}))
    runtime = _runtime(build_fake_anthropic(router.dispatch), session_factory=db_session_factory)
    register_agent_chat_tools(mcp, runtime, billing_config=None)

    with patch(
        "daimon.adapters.mcp.tools.agent_chat.create_session",
        new=AsyncMock(),
    ) as mock_create_session:
        result = await _call_tool_via_http(mcp.http_app(), token, tool_name, {"message": "hello"})

    payload = result.get("result", result)
    assert isinstance(payload, dict), f"unexpected tools/call shape: {result!r}"
    assert payload.get("isError"), (
        f"{tool_name} should refuse an over-balance tenant; got {payload!r}"
    )
    content = str(payload.get("content"))
    assert "TERMINAL ERROR" in content, f"refusal should be a TERMINAL ERROR; got {content!r}"
    assert "/billing" in content, f"refusal should name /billing; got {content!r}"
    mock_create_session.assert_not_awaited()


# ---------------------------------------------------------------------------
# Task 3: turn boundary (start_turn/continue_turn), list_events passthrough,
# archive_my_session
# ---------------------------------------------------------------------------


def _agent_and_env_router() -> MARouter:
    """Router with one agent + one environment, for start_turn's happy path."""
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
    router.add("GET", r"/v1/environments", lambda _r, _m: list_response([env_payload]))
    return router


def _isolated_agent_and_env_router(*, ma_agent_id: str = _MA_AGENT_ID) -> MARouter:
    """Router with one ISOLATED agent (``daimon_isolated="true"``) + one environment."""
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
                    id=ma_agent_id,
                    name="reader-agent",
                    metadata={
                        "daimon_tenant": str(_TENANT_ID),
                        "daimon_name": "reader-agent",
                        "daimon_isolated": "true",
                    },
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/environments", lambda _r, _m: list_response([env_payload]))
    return router


def _file_metadata_payload(file_id: str) -> dict[str, Any]:
    """A minimal valid ``FileMetadata`` payload for a ``retrieve_metadata`` fake."""
    return FileMetadata.model_validate(
        {
            "id": file_id,
            "created_at": "2026-09-01T00:00:00Z",
            "filename": "bundle.tar.gz",
            "mime_type": "application/gzip",
            "size_bytes": 1024,
            "type": "file",
        }
    ).model_dump(mode="json")


def _mint_bundle(
    *,
    secret: str = _BUNDLE_SECRET,
    file_id: str = "file_bundle_001",
    tenant_id: uuid.UUID = _TENANT_ID,
    agent_id: uuid.UUID = _AGENT_UUID,
) -> str:
    # `now` is real wall-clock time, not a fixed calendar date: a fixed past
    # date plus a fixed ttl eventually crosses its own expiry as the test
    # suite ages (observed 2026-09-08, one week after this helper landed).
    return bundle_handle.mint(
        secret,
        file_id=file_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        sha256="a" * 64,
        now=dt.datetime.now(dt.UTC),
        ttl_days=7,
    )


# ---------------------------------------------------------------------------
# Task 21-07: start_turn(bundle=) — verified mount, three refusals
# ---------------------------------------------------------------------------


async def test_start_turn_with_bundle_mounts_the_single_resource_on_an_isolated_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A verified bundle mounts as the ONLY resource, no vault_ids key at all."""
    create_bodies: list[dict[str, Any]] = []

    def on_create(request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        create_bodies.append(json_body(request))
        return httpx.Response(200, json=_make_fake_session(status="running"))

    router = _isolated_agent_and_env_router()
    router.add("POST", r"/v1/sessions", on_create)
    router.add(
        "GET",
        r"/v1/files/([^/]+)",
        lambda _r, m: httpx.Response(200, json=_file_metadata_payload(m.group(1))),
    )
    router.add(
        "POST",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: send_events_response(
            data=[
                BetaManagedAgentsUserMessageEvent(
                    id="sevt_bundle_boundary",
                    content=[
                        BetaManagedAgentsTextBlock(type="text", text="what does this report say?")
                    ],
                    type="user.message",
                    processed_at=dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.UTC),
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    runtime.settings.mcp.jwt_secret = SecretStr(_BUNDLE_SECRET)
    auth = _auth()
    handle = _mint_bundle(file_id="file_bundle_001")

    await _start_turn_impl(runtime, auth, "what does this report say?", handle)

    assert len(create_bodies) == 1, "exactly one session-create call should reach MA"
    body = create_bodies[0]
    assert body["resources"] == [
        {"type": "file", "file_id": "file_bundle_001", "mount_path": "/bundle.tar.gz"}
    ], f"the mounted resource must be exactly the bundle file, absolute path; got {body!r}"
    assert "vault_ids" not in body, "an isolated bundle session must never carry a vault_ids key"


async def test_start_turn_with_bundle_preserves_the_boundary_return_shape(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The bundle branch must not regress the plan 21-05 boundary return shape.

    ``turn_started_at`` is the injected pre-send clock, not the echo's own
    (absent, on the real API) timestamp — the echo's ``processed_at`` here is
    set only to prove it is ignored, not read.
    """
    call_order: list[str] = []
    fixed_at = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.UTC)

    def now() -> dt.datetime:
        call_order.append("now")
        return fixed_at

    router = _isolated_agent_and_env_router()
    router.add(
        "POST", r"/v1/sessions", lambda _r, _m: httpx.Response(200, json=_make_fake_session())
    )
    router.add(
        "GET",
        r"/v1/files/([^/]+)",
        lambda _r, m: httpx.Response(200, json=_file_metadata_payload(m.group(1))),
    )

    def on_send(_r: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        call_order.append("send")
        return send_events_response(
            data=[
                BetaManagedAgentsUserMessageEvent(
                    id="sevt_bundle_boundary_2",
                    content=[BetaManagedAgentsTextBlock(type="text", text="hi")],
                    type="user.message",
                    processed_at=None,
                ).model_dump(mode="json")
            ]
        )

    router.add("POST", r"/v1/sessions/([^/]+)/events", on_send)
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    runtime.settings.mcp.jwt_secret = SecretStr(_BUNDLE_SECRET)
    auth = _auth()
    handle = _mint_bundle()

    result = await _start_turn_impl(runtime, auth, "hi", handle, now=now)

    assert set(result) == {"handle", "turn_event_id", "turn_started_at"}, (
        f"bundle branch must return the same three-key boundary shape; got {result!r}"
    )
    assert result["turn_event_id"] == "sevt_bundle_boundary_2"
    assert result["turn_started_at"] == fixed_at.isoformat(), (
        "turn_started_at must be the injected clock, not the echo's processed_at"
    )
    assert call_order == ["now", "send"], (
        f"the clock must be read BEFORE events.send, not after; got {call_order!r}"
    )


def _zero_upstream_router() -> tuple[MARouter, list[str], list[str]]:
    """An isolated-agent router with counting (never-should-fire) session-create and
    files.retrieve_metadata routes, for the four handle-refusal tests."""
    create_calls: list[str] = []
    metadata_calls: list[str] = []
    router = _isolated_agent_and_env_router()
    router.add(
        "POST",
        r"/v1/sessions",
        lambda r, _m: (create_calls.append(json_body(r).get("agent", "")), httpx.Response(200))[1],
    )
    router.add(
        "GET",
        r"/v1/files/([^/]+)",
        lambda _r, m: (metadata_calls.append(m.group(1)), httpx.Response(200))[1],
    )
    return router, create_calls, metadata_calls


async def test_start_turn_with_bundle_from_different_tenant_is_refused_as_not_found(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    router, create_calls, metadata_calls = _zero_upstream_router()
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    runtime.settings.mcp.jwt_secret = SecretStr(_BUNDLE_SECRET)
    auth = _auth()
    handle = _mint_bundle(tenant_id=uuid.uuid4())

    with pytest.raises(ToolError, match="bundle not found"):
        await _start_turn_impl(runtime, auth, "hi", handle)

    assert create_calls == [], "a wrong-tenant handle must never reach session creation"
    assert metadata_calls == [], "a wrong-tenant handle must never reach the Files API"


async def test_start_turn_with_bundle_from_different_agent_is_refused_as_not_found(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    router, create_calls, metadata_calls = _zero_upstream_router()
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    runtime.settings.mcp.jwt_secret = SecretStr(_BUNDLE_SECRET)
    auth = _auth()
    handle = _mint_bundle(agent_id=uuid.uuid4())

    with pytest.raises(ToolError, match="bundle not found"):
        await _start_turn_impl(runtime, auth, "hi", handle)

    assert create_calls == [], "a wrong-agent handle must never reach session creation"
    assert metadata_calls == [], "a wrong-agent handle must never reach the Files API"


async def test_start_turn_with_tampered_bundle_signature_is_refused_as_not_found(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    router, create_calls, metadata_calls = _zero_upstream_router()
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    runtime.settings.mcp.jwt_secret = SecretStr(_BUNDLE_SECRET)
    auth = _auth()
    valid = _mint_bundle()
    payload_b64, sig_b64 = valid.split(".")
    flipped_char = "a" if sig_b64[0] != "a" else "b"
    tampered = f"{payload_b64}.{flipped_char}{sig_b64[1:]}"

    with pytest.raises(ToolError, match="bundle not found"):
        await _start_turn_impl(runtime, auth, "hi", tampered)

    assert create_calls == [], "a tampered signature must never reach session creation"
    assert metadata_calls == [], "a tampered signature must never reach the Files API"


async def test_start_turn_with_expired_bundle_is_refused_as_not_found(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    router, create_calls, metadata_calls = _zero_upstream_router()
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    runtime.settings.mcp.jwt_secret = SecretStr(_BUNDLE_SECRET)
    auth = _auth()
    handle = bundle_handle.mint(
        _BUNDLE_SECRET,
        file_id="file_bundle_001",
        tenant_id=_TENANT_ID,
        agent_id=_AGENT_UUID,
        sha256="a" * 64,
        now=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        ttl_days=1,
    )

    with pytest.raises(ToolError, match="bundle not found"):
        await _start_turn_impl(runtime, auth, "hi", handle)

    assert create_calls == [], "an expired handle must never reach session creation"
    assert metadata_calls == [], "an expired handle must never reach the Files API"


async def test_start_turn_with_bundle_whose_file_is_gone_is_refused_as_expired(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The Files API says the object is gone: a distinct, actionable message."""
    create_calls: list[str] = []
    router = _isolated_agent_and_env_router()
    router.add(
        "POST",
        r"/v1/sessions",
        lambda r, _m: (create_calls.append(json_body(r).get("agent", "")), httpx.Response(200))[1],
    )
    router.add(
        "GET",
        r"/v1/files/([^/]+)",
        lambda _r, _m: httpx.Response(
            404,
            json={
                "type": "error",
                "error": {"type": "not_found_error", "message": "file already gone"},
            },
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    runtime.settings.mcp.jwt_secret = SecretStr(_BUNDLE_SECRET)
    auth = _auth()
    handle = _mint_bundle(file_id="file_gone")

    with pytest.raises(ToolError, match="bundle expired; re-upload"):
        await _start_turn_impl(runtime, auth, "hi", handle)

    assert create_calls == [], "a gone bundle object must never reach session creation"


async def test_start_turn_with_bundle_on_non_isolated_agent_is_refused_before_verifying_handle(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The isolation check fires BEFORE handle verification — proven with a bad
    handle: if the isolation check ran second, this handle would fail
    ``bundle_handle.verify`` first and raise "bundle not found" instead. Only
    checking isolation FIRST produces "bundle requires an isolated agent" here.
    Also proven by a mutation: moving the isolation check after
    ``bundle_handle.verify`` makes the metadata_calls assertion below fail too,
    since a wrong-tenant handle would then be rejected before ever reaching
    this assertion's message check."""
    metadata_calls: list[str] = []
    router = _agent_and_env_router()  # NOT isolated — no daimon_isolated metadata
    router.add(
        "GET",
        r"/v1/files/([^/]+)",
        lambda _r, m: (metadata_calls.append(m.group(1)), httpx.Response(200))[1],
    )
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    runtime.settings.mcp.jwt_secret = SecretStr(_BUNDLE_SECRET)
    auth = _auth()
    handle = _mint_bundle(tenant_id=uuid.uuid4())  # a BAD handle — proves the order

    with pytest.raises(ToolError, match="bundle requires an isolated agent"):
        await _start_turn_impl(runtime, auth, "hi", handle)

    assert metadata_calls == [], (
        "the isolation check must fire before the handle is ever verified against the Files API"
    )


async def test_start_turn_with_bundle_when_jwt_secret_unset_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unverifiable handle (no configured secret) must never be accepted."""
    router = _isolated_agent_and_env_router()
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    assert runtime.settings.mcp.jwt_secret is None
    auth = _auth()
    handle = _mint_bundle()

    with pytest.raises(ToolError, match="not configured"):
        await _start_turn_impl(runtime, auth, "hi", handle)


async def test_start_turn_returns_the_accepted_events_boundary(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """turn_event_id comes from events.send's data[0]; turn_started_at from the
    caller's own clock, captured before the send, not from the echo.

    Also proves branch B (no ``bundle``) is untouched by the 21-07 bundle
    branch: ``create_session`` still receives the full vault/repo/env
    argument set, not the isolated path's stripped-down call.
    """
    call_order: list[str] = []
    fixed_at = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.UTC)

    def now() -> dt.datetime:
        call_order.append("now")
        return fixed_at

    router = _agent_and_env_router()

    def on_send(_r: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        call_order.append("send")
        return send_events_response(
            data=[
                BetaManagedAgentsUserMessageEvent(
                    id="sevt_boundary_001",
                    content=[BetaManagedAgentsTextBlock(type="text", text="hi")],
                    type="user.message",
                    processed_at=None,
                ).model_dump(mode="json")
            ]
        )

    router.add("POST", r"/v1/sessions/([^/]+)/events", on_send)
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    auth = _auth()
    fake_session = BetaManagedAgentsSession.model_validate(_make_fake_session(status="running"))

    with patch(
        "daimon.adapters.mcp.tools.agent_chat.create_session",
        new=AsyncMock(return_value=fake_session),
    ) as mock_create_session:
        result = await _start_turn_impl(runtime, auth, "hi", now=now)

    assert result["turn_event_id"] == "sevt_boundary_001", (
        f"turn_event_id should be the send response's accepted event id; got {result!r}"
    )
    assert result["turn_started_at"] == fixed_at.isoformat(), (
        f"turn_started_at should be the injected pre-send clock, isoformat()'d; got {result!r}"
    )
    assert call_order == ["now", "send"], (
        f"the clock must be read BEFORE events.send, not after; got {call_order!r}"
    )
    assert mock_create_session.await_args is not None, "create_session should be awaited once"
    call_kwargs = mock_create_session.await_args.kwargs
    assert set(call_kwargs) == {
        "agent",
        "environment",
        "mcp_settings",
        "account_id",
        "tenant_id",
        "agent_uuid",
        "session_factory",
        "fernet",
        "github_fallback_pat",
        "github_app_id",
        "github_app_private_key",
    }, f"the non-bundle path must keep passing its full argument set; got {sorted(call_kwargs)!r}"


async def test_continue_turn_returns_boundary_from_its_own_send() -> None:
    """continue_turn's turn_event_id is THIS send's accepted event, not session
    history; turn_started_at is the caller's own clock, captured before THIS
    send, not the echo's timestamp (the live API never populates one on the
    echo)."""
    call_order: list[str] = []
    own_clock_at = dt.datetime(2026, 9, 1, 11, 0, tzinfo=dt.UTC)

    def now() -> dt.datetime:
        call_order.append("now")
        return own_clock_at

    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(200, json=_make_fake_session(status="idle")),
    )

    def on_send(_r: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        call_order.append("send")
        return send_events_response(
            data=[
                BetaManagedAgentsUserMessageEvent(
                    id="sevt_continue_boundary",
                    content=[BetaManagedAgentsTextBlock(type="text", text="again")],
                    type="user.message",
                    processed_at=None,
                ).model_dump(mode="json")
            ]
        )

    router.add("POST", r"/v1/sessions/([^/]+)/events", on_send)
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    result = await _continue_turn_impl(runtime, auth, "ses_test001", "again", now=now)

    assert result == {
        "handle": "ses_test001",
        "turn_event_id": "sevt_continue_boundary",
        "turn_started_at": own_clock_at.isoformat(),
    }
    assert call_order == ["now", "send"], (
        f"the clock must be read BEFORE events.send, not after; got {call_order!r}"
    )


async def test_start_turn_raises_when_send_accepts_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A send response with data=None means nothing was accepted — not a started turn."""
    router = _agent_and_env_router()
    router.add(
        "POST",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: send_events_response(data=None),
    )
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    auth = _auth()
    fake_session = BetaManagedAgentsSession.model_validate(_make_fake_session(status="running"))

    with (
        patch(
            "daimon.adapters.mcp.tools.agent_chat.create_session",
            new=AsyncMock(return_value=fake_session),
        ),
        pytest.raises(ToolError, match="send returned no accepted event"),
    ):
        await _start_turn_impl(runtime, auth, "hi")


async def test_start_turn_ignores_the_echos_processed_at_even_when_present(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """turn_started_at never reads the echo's processed_at, present or absent.

    The live API always echoes ``processed_at=None`` on the send response (a
    timestamp appears only ~0.5s later, once the agent starts on the event),
    so the boundary's clock can only ever be the caller's own, captured
    before the send. This test pins a non-None processed_at on the fake echo
    specifically to prove it is never read.
    """
    injected_at = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)
    echoed_processed_at = dt.datetime(1999, 1, 1, tzinfo=dt.UTC)
    router = _agent_and_env_router()
    router.add(
        "POST",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: send_events_response(
            data=[
                BetaManagedAgentsUserMessageEvent(
                    id="sevt_no_timestamp",
                    content=[BetaManagedAgentsTextBlock(type="text", text="hi")],
                    type="user.message",
                    processed_at=echoed_processed_at,
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client, session_factory=db_session_factory, environment_name=_ENV_NAME)
    auth = _auth()
    fake_session = BetaManagedAgentsSession.model_validate(_make_fake_session(status="running"))

    with patch(
        "daimon.adapters.mcp.tools.agent_chat.create_session",
        new=AsyncMock(return_value=fake_session),
    ):
        result = await _start_turn_impl(runtime, auth, "hi", now=lambda: injected_at)

    assert result["turn_event_id"] == "sevt_no_timestamp"
    assert result["turn_started_at"] == injected_at.isoformat(), (
        "turn_started_at must be the injected clock, never the echo's processed_at"
    )


async def test_list_events_forwards_created_at_gte_and_types() -> None:
    """created_at_gte and types both reach the SDK request when given."""
    captured: list[httpx.Request] = []

    def on_events(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"data": [], "next_page": None})

    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(200, json=_make_fake_session(status="idle")),
    )
    router.add("GET", r"/v1/sessions/([^/]+)/events", on_events)
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    await _list_events_impl(
        runtime,
        auth,
        "ses_test001",
        None,
        None,
        "asc",
        "2026-09-01T10:00:00Z",
        ["agent.message", "session.status_idle"],
    )

    assert len(captured) == 1, f"expected exactly one events.list call; got {len(captured)}"
    query = captured[0].url.params
    assert query.get("created_at[gte]") == "2026-09-01T10:00:00Z", (
        f"created_at_gte should reach the wire as created_at[gte]; got {dict(query)!r}"
    )
    assert query.get_list("types[]") == ["agent.message", "session.status_idle"], (
        f"types should reach the wire as repeated types[] params; got {dict(query)!r}"
    )


async def test_list_events_forwards_neither_when_not_given() -> None:
    """Without created_at_gte/types, neither key reaches the SDK request (conditional, like page/limit/order)."""
    captured: list[httpx.Request] = []

    def on_events(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"data": [], "next_page": None})

    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(200, json=_make_fake_session(status="idle")),
    )
    router.add("GET", r"/v1/sessions/([^/]+)/events", on_events)
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    await _list_events_impl(runtime, auth, "ses_test001", None, None, None)

    assert len(captured) == 1
    query = captured[0].url.params
    assert "created_at[gte]" not in query, (
        f"unset created_at_gte must not be forwarded; got {dict(query)!r}"
    )
    assert "types[]" not in query, f"unset types must not be forwarded; got {dict(query)!r}"


async def test_archive_my_session_rejects_a_sibling_agents_session_and_issues_no_archive_call() -> (
    None
):
    """Ownership is checked before archiving — a sibling's session is refused with
    zero archive calls (T-21-05-A)."""
    archive_calls: list[str] = []

    def on_archive(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        archive_calls.append(m.group(1))
        return httpx.Response(
            200,
            json=_make_fake_session(
                session_id="ses_sibling", agent_id="ag_sibling", status="terminated"
            ),
        )

    router = _sibling_tenant_agents_router()
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=_make_fake_session(session_id="ses_sibling", agent_id="ag_sibling", status="idle"),
        ),
    )
    router.add("POST", r"/v1/sessions/([^/]+)/archive", on_archive)
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    with pytest.raises(ToolError, match="session not found"):
        await _archive_my_session_impl(runtime, auth, "ses_sibling")

    assert archive_calls == [], "a rejected ownership check must issue no archive call"


async def test_archive_my_session_archives_an_owned_session_exactly_once() -> None:
    """An owned session is archived with exactly one call to the archive endpoint."""
    archive_calls: list[str] = []

    def on_archive(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        archive_calls.append(m.group(1))
        return httpx.Response(200, json=_make_fake_session(status="terminated"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(200, json=_make_fake_session(status="idle")),
    )
    router.add("POST", r"/v1/sessions/([^/]+)/archive", on_archive)
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    result = await _archive_my_session_impl(runtime, auth, "ses_test001")

    assert result == {"handle": "ses_test001", "archived": "true"}
    assert archive_calls == ["ses_test001"], (
        f"expected exactly one archive call; got {archive_calls!r}"
    )


# ---------------------------------------------------------------------------
# cancel_turn — one unconditional interrupt, then report status
# ---------------------------------------------------------------------------


async def test_cancel_turn_sends_exactly_one_user_interrupt_event() -> None:
    """The send the fake receives has exactly one event, and its type is
    ``user.interrupt`` — asserted on the captured request body."""
    captured: list[dict[str, Any]] = []

    def on_send(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.append(json_body(req))
        return send_events_response(data=[])

    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(200, json=_make_fake_session(status="running")),
    )
    router.add("POST", r"/v1/sessions/([^/]+)/events", on_send)
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    await _cancel_turn_impl(runtime, auth, "ses_test001")

    assert len(captured) == 1, f"expected exactly one send call; got {len(captured)}"
    assert captured[0]["events"] == [{"type": "user.interrupt"}], (
        f"cancel_turn must send exactly one user.interrupt event; got {captured[0]!r}"
    )


async def test_cancel_turn_issues_no_status_precheck_between_ownership_and_send() -> None:
    """No read-then-send race (T-21-06-B): the recorded call order is
    ownership-retrieve, send, status-retrieve — never an extra status read
    wedged in front of the send."""
    calls: list[str] = []

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        calls.append("retrieve")
        return httpx.Response(200, json=_make_fake_session(status="running"))

    def on_send(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        calls.append("send")
        return send_events_response(data=[])

    router = MARouter()
    router.add("GET", r"/v1/sessions/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/sessions/([^/]+)/events", on_send)
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    result = await _cancel_turn_impl(runtime, auth, "ses_test001")

    assert calls == ["retrieve", "send", "retrieve"], (
        "expected ownership-retrieve, send, status-retrieve in that order with no "
        f"extra status read between the ownership check and the send; got {calls!r}"
    )
    assert result == {"handle": "ses_test001", "status": "running"}


async def test_cancel_turn_rejects_a_sibling_agents_session_and_issues_zero_sends() -> None:
    """Ownership is checked before any send — a sibling's session is refused
    with zero interrupt sends (T-21-06-A)."""
    send_calls: list[str] = []

    def on_send(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        send_calls.append(m.group(1))
        return send_events_response(data=[])

    router = _sibling_tenant_agents_router()
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=_make_fake_session(
                session_id="ses_sibling", agent_id="ag_sibling", status="running"
            ),
        ),
    )
    router.add("POST", r"/v1/sessions/([^/]+)/events", on_send)
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    with pytest.raises(ToolError, match="session not found"):
        await _cancel_turn_impl(runtime, auth, "ses_sibling")

    assert send_calls == [], "a rejected ownership check must issue no interrupt send"


async def test_cancel_turn_on_already_idle_session_returns_idle_without_raising() -> None:
    """Sending an interrupt to an already-idle session is harmless — no raise."""
    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(200, json=_make_fake_session(status="idle")),
    )
    router.add(
        "POST",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: send_events_response(data=[]),
    )
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    result = await _cancel_turn_impl(runtime, auth, "ses_test001")

    assert result == {"handle": "ses_test001", "status": "idle"}


# ---------------------------------------------------------------------------
# get_turn_cost — fold one turn's model-request events, pre-markup
# ---------------------------------------------------------------------------


def _cost_event(
    *,
    event_id: str,
    usage: BetaManagedAgentsSpanModelUsage,
    processed_at: dt.datetime,
) -> dict[str, Any]:
    """Build a real ``span.model_request_end`` event payload inline."""
    return BetaManagedAgentsSpanModelRequestEndEvent(
        id=event_id,
        model_request_start_id=f"{event_id}_start",
        model_usage=usage,
        processed_at=processed_at,
        type="span.model_request_end",
    ).model_dump(mode="json")


async def test_get_turn_cost_folds_events_to_the_same_figure_as_debit_amount_at_markup_one() -> (
    None
):
    """The fold equals sum(debit_amount(cost_of(usage, rates), markup=1)) over
    the same events — the pre-markup cross-check SPEC 1.4 asks for."""
    rates = MODEL_PRICING["claude-sonnet-4-6"]
    usage_a = BetaManagedAgentsSpanModelUsage(
        input_tokens=1000,
        output_tokens=500,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    usage_b = BetaManagedAgentsSpanModelUsage(
        input_tokens=2000,
        output_tokens=100,
        cache_creation_input_tokens=50,
        cache_read_input_tokens=10,
    )
    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(200, json=_make_fake_session(status="idle")),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: list_response(
            [
                _cost_event(
                    event_id="sevt_cost_a",
                    usage=usage_a,
                    processed_at=dt.datetime(2026, 9, 1, 10, 0, 1, tzinfo=dt.UTC),
                ),
                _cost_event(
                    event_id="sevt_cost_b",
                    usage=usage_b,
                    processed_at=dt.datetime(2026, 9, 1, 10, 0, 2, tzinfo=dt.UTC),
                ),
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    result = await _get_turn_cost_impl(
        runtime, auth, "ses_test001", "2026-09-01T10:00:00Z", "sevt_boundary"
    )

    expected = sum(
        (debit_amount(cost_of(usage, rates), markup=Decimal(1)) for usage in (usage_a, usage_b)),
        start=Decimal("0"),
    )
    assert result["cost_usd"] == str(expected), (
        f"fold should equal sum(debit_amount(cost_of(usage, rates), markup=1)); got {result!r}"
    )
    assert result["event_count"] == 2


async def test_get_turn_cost_excludes_the_boundary_event_itself() -> None:
    """Events at or before ``turn_event_id`` are excluded: of three seeded
    events, one IS the boundary, so ``event_count`` is 2, not 3."""
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=100,
        output_tokens=50,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(200, json=_make_fake_session(status="idle")),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: list_response(
            [
                _cost_event(
                    event_id="sevt_boundary",
                    usage=usage,
                    processed_at=dt.datetime(2026, 9, 1, 10, 0, 0, tzinfo=dt.UTC),
                ),
                _cost_event(
                    event_id="sevt_a",
                    usage=usage,
                    processed_at=dt.datetime(2026, 9, 1, 10, 0, 1, tzinfo=dt.UTC),
                ),
                _cost_event(
                    event_id="sevt_b",
                    usage=usage,
                    processed_at=dt.datetime(2026, 9, 1, 10, 0, 2, tzinfo=dt.UTC),
                ),
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    result = await _get_turn_cost_impl(
        runtime, auth, "ses_test001", "2026-09-01T10:00:00Z", "sevt_boundary"
    )

    assert result["event_count"] == 2, (
        f"the boundary event itself must be excluded from the fold; got {result!r}"
    )


async def test_get_turn_cost_returns_none_but_still_counts_events_for_unpriced_model() -> None:
    """No pricing row -> cost_usd is None (never zero — zero would falsely
    claim the turn was free); event_count still counts the events (T-21-06-D)."""
    session_json = BetaManagedAgentsSession.model_validate(
        {
            "id": "ses_test001",
            "type": "session",
            "agent": {
                "id": _MA_AGENT_ID,
                "name": "test-agent",
                "version": 1,
                "type": "agent",
                "model": {"id": "claude-unpriced-model-x"},
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
            "status": "idle",
            "title": None,
            "usage": {},
            "vault_ids": [],
        }
    ).model_dump(mode="json")
    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=100,
        output_tokens=50,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    router = MARouter()
    router.add(
        "GET", r"/v1/sessions/([^/]+)", lambda _r, _m: httpx.Response(200, json=session_json)
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: list_response(
            [
                _cost_event(
                    event_id="sevt_unpriced",
                    usage=usage,
                    processed_at=dt.datetime(2026, 9, 1, 10, 0, 1, tzinfo=dt.UTC),
                )
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    result = await _get_turn_cost_impl(
        runtime, auth, "ses_test001", "2026-09-01T10:00:00Z", "sevt_boundary"
    )

    assert result == {"cost_usd": None, "event_count": 1}, (
        f"an unpriced model must yield None cost while still counting events; got {result!r}"
    )


async def test_get_turn_cost_returns_a_real_zero_when_priced_model_has_no_events_yet() -> None:
    """A priced model with no span.model_request_end events yet returns a
    real zero — distinct from the unpriced None case."""
    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(200, json=_make_fake_session(status="idle")),
    )
    router.add("GET", r"/v1/sessions/([^/]+)/events", lambda _r, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    result = await _get_turn_cost_impl(
        runtime, auth, "ses_test001", "2026-09-01T10:00:00Z", "sevt_boundary"
    )

    assert result == {"cost_usd": "0", "event_count": 0}, (
        f"a priced model with no events yet must return a real zero, not None; got {result!r}"
    )


async def test_get_turn_cost_rejects_a_sibling_agents_session_and_lists_no_events() -> None:
    """Ownership is checked before any events read — a sibling's session is
    refused with zero events.list calls."""
    events_calls: list[str] = []

    def on_events(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        events_calls.append(m.group(1))
        return list_response([])

    router = _sibling_tenant_agents_router()
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=_make_fake_session(session_id="ses_sibling", agent_id="ag_sibling", status="idle"),
        ),
    )
    router.add("GET", r"/v1/sessions/([^/]+)/events", on_events)
    client = build_fake_anthropic(router.dispatch)
    runtime = _runtime(client)
    auth = _auth()

    with pytest.raises(ToolError, match="session not found"):
        await _get_turn_cost_impl(
            runtime, auth, "ses_sibling", "2026-09-01T10:00:00Z", "sevt_boundary"
        )

    assert events_calls == [], "a rejected ownership check must issue no events.list call"
