"""Tests for daimon.core.headless_runner.run_turn.

Non-interactive turn driver — opens an MA session, sends a single trigger
message, drains SSE through the turn-state reducers until terminal `status_idle`,
returns the truncated final-message tail.

Tests use transport-level fakes via build_fake_anthropic and MARouter from
daimon.testing.ma. SDK event and session objects are constructed inline at
each call site per guideline:testing — no factories, no model_construct.

All tests include a terminal status_idle event that triggers `break` in
`run_turn` before the stream closes. With transport-level SSE (where the HTTP
response body ends after all events), the SDK's stream parser closes the
iterator naturally. Since `run_turn` breaks on `status_idle` before reaching
the end, the closed-stream behavior is equivalent. The old _FakeAsyncIter
blocking behavior is not needed.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import anthropic
import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsSession,
    BetaManagedAgentsSessionAgent,
    FileMetadata,
)
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.sessions import BetaManagedAgentsSessionEvent
from anthropic.types.beta.sessions.beta_managed_agents_agent_message_event import (
    BetaManagedAgentsAgentMessageEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_retry_status_terminal import (
    BetaManagedAgentsRetryStatusTerminal,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_error_event import (
    BetaManagedAgentsSessionErrorEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_requires_action import (
    BetaManagedAgentsSessionRequiresAction,
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
from anthropic.types.beta.sessions.beta_managed_agents_unknown_error import (
    BetaManagedAgentsUnknownError,
)
from cryptography.fernet import Fernet
from daimon.core.config import McpSettings
from daimon.core.github_credentials import build_multifernet, upsert_credential_encrypted
from daimon.core.headless_runner import LAST_RESULT_TAIL_MAX, run_turn
from daimon.core.stores import agent_github_binding as github_binding_store
from daimon.core.stores import agent_repo_binding as repo_binding_store
from daimon.core.stores.agent_files import put_agent_file
from daimon.testing.factories import make_tenant
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    EMPTY_SESSION_STATS,
    EMPTY_SESSION_USAGE,
    MARouter,
    build_fake_anthropic,
    sse_response,
)
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=dt.UTC)
_SESSION_ID = "ses_test"
_MODEL_ID = "claude-sonnet-4-5"


def _fake_session_json(session_id: str = _SESSION_ID, model_id: str = _MODEL_ID) -> dict[str, Any]:
    """Build a real BetaManagedAgentsSession payload for POST /v1/sessions responses."""
    return BetaManagedAgentsSession(
        outcome_evaluations=[],
        id=session_id,
        type="session",
        status="idle",
        environment_id="env_x",
        metadata={},
        resources=[],
        vault_ids=[],
        created_at=_NOW,
        updated_at=_NOW,
        stats=EMPTY_SESSION_STATS,
        usage=EMPTY_SESSION_USAGE,
        agent=BetaManagedAgentsSessionAgent(
            id="agent_x",
            type="agent",
            name="test-agent",
            version=1,
            model=BetaManagedAgentsModelConfig(id=model_id),  # type: ignore[arg-type]
            mcp_servers=[],
            tools=[],
            skills=[],
        ),
    ).model_dump(mode="json")


def _register_agent_environment_routes(router: MARouter, *, model_id: str = _MODEL_ID) -> None:
    """Serve GET /v1/agents/{id} and GET /v1/environments/{id} with validated
    SDK payloads.

    run_turn bridges its string agent_id/environment_id to the SDK objects
    create_session needs via beta.agents.retrieve / beta.environments.retrieve
    before delegating — every router a run_turn test builds needs these two
    routes now, regardless of which id string the test passes.
    """

    def handle_agent_retrieve(request: httpx.Request, match: Any) -> httpx.Response:
        agent = BetaManagedAgentsAgent(
            id=match.group(1),
            type="agent",
            name="test-agent",
            version=1,
            model=BetaManagedAgentsModelConfig(id=model_id),  # type: ignore[arg-type]
            mcp_servers=[],
            tools=[],
            skills=[],
            metadata={},
            created_at=_NOW,
            updated_at=_NOW,
        )
        return httpx.Response(200, json=agent.model_dump(mode="json"))

    def handle_environment_retrieve(request: httpx.Request, match: Any) -> httpx.Response:
        environment = BetaEnvironment(
            id=match.group(1),
            type="environment",
            name="test-env",
            config=EMPTY_CLOUD_CONFIG,
            created_at=_NOW.isoformat(),
            updated_at=_NOW.isoformat(),
            description="",
            metadata={},
        )
        return httpx.Response(200, json=environment.model_dump(mode="json"))

    router.add("GET", r"/v1/agents/([^/]+)", handle_agent_retrieve)
    router.add("GET", r"/v1/environments/([^/]+)", handle_environment_retrieve)


def _build_client(
    events: list[BetaManagedAgentsSessionEvent],
    *,
    session_id: str = _SESSION_ID,
    model_id: str = _MODEL_ID,
    send_capture: list[dict[str, Any]] | None = None,
    session_create_capture: list[dict[str, Any]] | None = None,
) -> AsyncAnthropic:
    """Build transport-level client with session create + stream + send handlers.

    If `send_capture` is provided (a list), all POST /v1/sessions/{id}/events
    request bodies are appended to it for assertion. If `session_create_capture`
    is provided, the POST /v1/sessions request body is appended to it.
    """
    router = MARouter()
    session_json = _fake_session_json(session_id=session_id, model_id=model_id)
    event_dicts = [e.model_dump(mode="json") for e in events]

    def handle_create(request: httpx.Request, match: Any) -> httpx.Response:
        if session_create_capture is not None:
            session_create_capture.append(json.loads(request.content))
        return httpx.Response(200, json=session_json)

    def handle_stream(request: httpx.Request, match: Any) -> httpx.Response:
        return sse_response(event_dicts)

    def handle_send(request: httpx.Request, match: Any) -> httpx.Response:
        if send_capture is not None:
            body: dict[str, Any] = json.loads(request.content)
            send_capture.append(body)
        return httpx.Response(200, json={"data": None})

    router.add("POST", r"/v1/sessions", handle_create)
    router.add("GET", r"/v1/sessions/[^/]+/events/stream", handle_stream)
    router.add("POST", r"/v1/sessions/[^/]+/events", handle_send)
    _register_agent_environment_routes(router, model_id=model_id)
    return build_fake_anthropic(router.dispatch)


async def test_run_turn_returns_last_message_text() -> None:
    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsAgentMessageEvent(
            id="evt_msg_1",
            type="agent.message",
            processed_at=_NOW,
            content=[BetaManagedAgentsTextBlock(type="text", text="hello world")],
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id="evt_idle_1",
            type="session.status_idle",
            processed_at=_NOW,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    ]
    client = _build_client(events)

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
    )

    assert tail == "hello world", "run_turn should return the agent.message text"


async def test_tail_truncated_at_1000() -> None:
    long_text = "a" * 2000
    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsAgentMessageEvent(
            id="evt_msg_1",
            type="agent.message",
            processed_at=_NOW,
            content=[BetaManagedAgentsTextBlock(type="text", text=long_text)],
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id="evt_idle_1",
            type="session.status_idle",
            processed_at=_NOW,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    ]
    client = _build_client(events)

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
    )

    assert len(tail) == LAST_RESULT_TAIL_MAX == 1000, (
        "tail must be truncated to LAST_RESULT_TAIL_MAX (1000) chars"
    )


async def test_auto_allow_requires_action_idempotent() -> None:
    """Two requires_action idle events naming the same blocked id should
    only produce one tool_confirmation send (no double-acks on re-emit).
    """
    send_capture: list[dict[str, Any]] = []

    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsSessionStatusIdleEvent(
            id="evt_idle_block_1",
            type="session.status_idle",
            processed_at=_NOW,
            stop_reason=BetaManagedAgentsSessionRequiresAction(
                type="requires_action",
                event_ids=["tu_x"],
            ),
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id="evt_idle_block_2",
            type="session.status_idle",
            processed_at=_NOW,
            stop_reason=BetaManagedAgentsSessionRequiresAction(
                type="requires_action",
                event_ids=["tu_x"],  # same blocked id re-emitted
            ),
        ),
        BetaManagedAgentsAgentMessageEvent(
            id="evt_msg_1",
            type="agent.message",
            processed_at=_NOW,
            content=[BetaManagedAgentsTextBlock(type="text", text="done")],
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id="evt_idle_done",
            type="session.status_idle",
            processed_at=_NOW,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    ]
    client = _build_client(events, send_capture=send_capture)

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
    )

    # send_capture has all POST /v1/sessions/{id}/events bodies:
    # [0] = trigger user.message
    # [1] = tool_confirmation for tu_x (should be exactly one)
    # There should NOT be a second tool_confirmation
    def _is_confirmation(body: dict[str, Any]) -> bool:
        events: list[dict[str, Any]] = body.get("events") or []
        return any(e.get("type") == "user.tool_confirmation" for e in events)

    confirmation_sends = [body for body in send_capture if _is_confirmation(body)]
    assert len(confirmation_sends) == 1, (
        f"expected exactly 1 tool_confirmation send; got {len(confirmation_sends)}"
    )
    confirmation_events = confirmation_sends[0]["events"]
    assert confirmation_events[0]["tool_use_id"] == "tu_x"
    assert confirmation_events[0]["result"] == "allow"
    assert confirmation_events[0]["type"] == "user.tool_confirmation"
    assert tail == "done", "tail should reflect the agent.message after unblocking"


async def test_session_error_raises() -> None:
    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsSessionErrorEvent(
            id="evt_err_1",
            type="session.error",
            processed_at=_NOW,
            error=BetaManagedAgentsUnknownError(
                type="unknown_error",
                message="boom",
                retry_status=BetaManagedAgentsRetryStatusTerminal(type="terminal"),
            ),
        ),
    ]
    client = _build_client(events)

    with pytest.raises(RuntimeError, match=r"^session\.error:"):
        await run_turn(
            anthropic=client,
            agent_id="agent_x",
            environment_id="env_x",
            trigger_message="hi",
        )


async def test_run_turn_calls_usage_record_for_span_model_request_end() -> None:
    from unittest.mock import AsyncMock

    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsSpanModelRequestEndEvent(
            id="evt_span_1",
            type="span.model_request_end",
            processed_at=_NOW,
            model_request_start_id="evt_span_start_1",
            model_usage=BetaManagedAgentsSpanModelUsage(
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                input_tokens=10,
                output_tokens=5,
            ),
        ),
        BetaManagedAgentsAgentMessageEvent(
            id="evt_msg_1",
            type="agent.message",
            processed_at=_NOW,
            content=[BetaManagedAgentsTextBlock(type="text", text="ok")],
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id="evt_idle_1",
            type="session.status_idle",
            processed_at=_NOW,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    ]
    usage_record = AsyncMock(return_value=None)
    client = _build_client(events, session_id="ses_abc")

    captured_factory_args: list[tuple[str, str]] = []

    def factory(session_id: str, model_id: str) -> AsyncMock:
        captured_factory_args.append((session_id, model_id))
        return usage_record

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        usage_record_factory=factory,
    )

    assert tail == "ok"
    assert captured_factory_args == [("ses_abc", _MODEL_ID)], (
        "factory should be called once with (session.id, session.agent.model.id)"
    )
    assert usage_record.await_count == 1, (
        "usage_record must be awaited once per span.model_request_end event"
    )
    call = usage_record.await_args
    assert call is not None
    assert call.kwargs["session_id"] == "ses_abc"
    assert call.kwargs["event"].id == "evt_span_1"


async def test_run_turn_propagates_usage_record_factory_exception() -> None:
    """If usage_record raises (fail-closed), run_turn lets it propagate."""
    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsSpanModelRequestEndEvent(
            id="evt_span_boom",
            type="span.model_request_end",
            processed_at=_NOW,
            model_request_start_id="evt_span_start_1",
            model_usage=BetaManagedAgentsSpanModelUsage(
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                input_tokens=10,
                output_tokens=5,
            ),
        ),
    ]
    client = _build_client(events)

    async def boom(*, event: object, session_id: str) -> None:
        del event, session_id
        raise RuntimeError("metering down")

    def factory(_session_id: str, _model_id: str) -> Callable[..., Awaitable[None]]:
        return boom

    with pytest.raises(RuntimeError, match="metering down"):
        await run_turn(
            anthropic=client,
            agent_id="agent_x",
            environment_id="env_x",
            trigger_message="hi",
            usage_record_factory=factory,
        )


async def test_run_turn_propagates_httpx_error() -> None:
    """A transport error from events.stream surfaces as anthropic.APIConnectionError."""
    router = MARouter()

    def handle_create(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(200, json=_fake_session_json())

    def handle_send(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(200, json={"data": None})

    def handle_stream(request: httpx.Request, match: Any) -> httpx.Response:
        raise httpx.ConnectError("boom")

    router.add("POST", r"/v1/sessions", handle_create)
    router.add("POST", r"/v1/sessions/[^/]+/events", handle_send)
    router.add("GET", r"/v1/sessions/[^/]+/events/stream", handle_stream)
    _register_agent_environment_routes(router)
    client = build_fake_anthropic(router.dispatch)

    with pytest.raises(anthropic.APIConnectionError):
        await run_turn(
            anthropic=client,
            agent_id="agent_x",
            environment_id="env_x",
            trigger_message="hi",
        )


async def test_run_turn_attaches_vault_when_mcp_settings() -> None:
    """Cold-path: ensure_agent_mcp_vault performs vaults.list → vaults.create, then
    run_turn passes the new per-agent vault id into beta.sessions.create as vault_ids.
    """
    account_id = uuid.uuid4()
    agent_uuid = uuid.uuid4()
    public_url = "https://mcp.example.local/mcp"
    visited_paths: list[tuple[str, str]] = []
    session_request_bodies: list[dict[str, Any]] = []

    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsAgentMessageEvent(
            id="evt_msg_1",
            type="agent.message",
            processed_at=_NOW,
            content=[BetaManagedAgentsTextBlock(type="text", text="ok")],
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id="evt_idle_1",
            type="session.status_idle",
            processed_at=_NOW,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    ]
    event_dicts = [e.model_dump(mode="json") for e in events]
    session_json = _fake_session_json()

    router = MARouter()

    def handle_vaults_list(request: httpx.Request, match: Any) -> httpx.Response:
        visited_paths.append((request.method, request.url.path))
        # Cold path: no existing vaults.
        return httpx.Response(200, json={"data": [], "has_more": False})

    def handle_vaults_create(request: httpx.Request, match: Any) -> httpx.Response:
        visited_paths.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "id": "vlt_new",
                "type": "vault",
                "display_name": f"daimon-mcp:{account_id}:{agent_uuid}",
                "metadata": None,
                "archived_at": None,
                "created_at": "2026-05-20T00:00:00Z",
            },
        )

    def handle_credentials_create(request: httpx.Request, match: Any) -> httpx.Response:
        visited_paths.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "id": "vcrd_new",
                "type": "vault_credential",
                "vault_id": "vlt_new",
                "metadata": {},
                "created_at": "2026-05-20T00:00:00Z",
                "updated_at": "2026-05-20T00:00:00Z",
                "auth": {"type": "static_bearer", "mcp_server_url": public_url},
            },
        )

    def handle_session_create(request: httpx.Request, match: Any) -> httpx.Response:
        visited_paths.append((request.method, request.url.path))
        session_request_bodies.append(json.loads(request.content))
        return httpx.Response(200, json=session_json)

    def handle_stream(request: httpx.Request, match: Any) -> httpx.Response:
        return sse_response(event_dicts)

    def handle_send(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(200, json={"data": None})

    router.add("GET", r"/v1/vaults", handle_vaults_list)
    router.add("POST", r"/v1/vaults", handle_vaults_create)
    router.add("POST", r"/v1/vaults/[^/]+/credentials", handle_credentials_create)
    router.add("POST", r"/v1/sessions", handle_session_create)
    router.add("GET", r"/v1/sessions/[^/]+/events/stream", handle_stream)
    router.add("POST", r"/v1/sessions/[^/]+/events", handle_send)
    _register_agent_environment_routes(router)
    client = build_fake_anthropic(router.dispatch)

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        mcp_settings=McpSettings(
            jwt_secret=SecretStr("x" * 32),
            public_url=HttpUrl(public_url),
        ),
        account_id=account_id,
        agent_uuid=agent_uuid,
    )

    assert tail == "ok"
    # Cold-path sequence: list → create → credentials.create, then sessions.create.
    assert ("GET", "/v1/vaults") in visited_paths, (
        "ensure_agent_mcp_vault must list vaults to check for an existing one"
    )
    assert ("POST", "/v1/vaults") in visited_paths, (
        "ensure_agent_mcp_vault must POST to create a new vault on the cold path"
    )
    # Order matters: GET must precede POST on /v1/vaults.
    list_idx = visited_paths.index(("GET", "/v1/vaults"))
    create_idx = visited_paths.index(("POST", "/v1/vaults"))
    assert list_idx < create_idx, "vaults.list must precede vaults.create"

    assert len(session_request_bodies) == 1
    assert session_request_bodies[0].get("vault_ids") == ["vlt_new"], (
        "beta.sessions.create must carry the per-agent ensured vault id"
    )


async def test_run_turn_raises_value_error_when_account_id_missing() -> None:
    """When mcp_settings carries both fields but account_id is None,
    run_turn must raise ValueError with the exact wording from sessions.py.
    """
    # The agent/environment retrieve bridge runs before create_session's
    # ValueError guard, so those two routes must exist even though no
    # session-create/events call is expected.
    router = MARouter()
    _register_agent_environment_routes(router)
    client = build_fake_anthropic(router.dispatch)

    with pytest.raises(
        ValueError,
        match=(r"^account_id is required when mcp_settings has public_url and jwt_secret$"),
    ):
        await run_turn(
            anthropic=client,
            agent_id="agent_x",
            environment_id="env_x",
            trigger_message="hi",
            mcp_settings=McpSettings(
                jwt_secret=SecretStr("x" * 32),
                public_url=HttpUrl("https://mcp.example.local/mcp"),
            ),
            account_id=None,
        )


async def test_run_turn_raises_when_mcp_active_and_agent_uuid_none() -> None:
    """SC-2c: run_turn must raise ValueError when mcp_settings is mcp-active
    (public_url + jwt_secret both set) and agent_uuid is None.

    The agent/environment retrieve bridge runs before create_session's
    ValueError guard, so those two routes must exist even though no
    session-create/events call is expected.
    """
    router = MARouter()
    _register_agent_environment_routes(router)
    client = build_fake_anthropic(router.dispatch)
    account_id = uuid.uuid4()

    with pytest.raises(
        ValueError,
        match=r"^agent_uuid is required when mcp_settings has public_url and jwt_secret$",
    ):
        await run_turn(
            anthropic=client,
            agent_id="agent_x",
            environment_id="env_x",
            trigger_message="hi",
            mcp_settings=McpSettings(
                jwt_secret=SecretStr("x" * 32),
                public_url=HttpUrl("https://mcp.example.local/mcp"),
            ),
            account_id=account_id,
            agent_uuid=None,
        )


async def test_run_turn_stamps_session_metadata_with_tenant_and_account() -> None:
    """A scheduler-fired (headless) session must carry
    daimon_tenant + daimon_account metadata, mirroring create_session's stamp
    (test_sessions.py::test_create_session_tags_metadata_with_account_and_tenant_when_both_provided),
    so sweep_headless_usage (usage_sweep.py:57-61) does not skip it and routine
    sessions get billed.
    """
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsAgentMessageEvent(
            id="evt_msg_1",
            type="agent.message",
            processed_at=_NOW,
            content=[BetaManagedAgentsTextBlock(type="text", text="ok")],
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id="evt_idle_1",
            type="session.status_idle",
            processed_at=_NOW,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    ]
    session_create_capture: list[dict[str, Any]] = []
    client = _build_client(events, session_create_capture=session_create_capture)

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        tenant_id=tenant_id,
        account_id=account_id,
    )

    assert tail == "ok"
    assert len(session_create_capture) == 1, "exactly one session-create call"
    metadata = session_create_capture[0].get("metadata")
    assert metadata is not None, (
        "session-create body must carry metadata when tenant_id/account_id are given"
    )
    assert metadata["daimon_tenant"] == str(tenant_id), (
        "metadata must tag daimon_tenant with the tenant UUID string"
    )
    assert metadata["daimon_account"] == str(account_id), (
        "metadata must tag daimon_account with the account UUID string"
    )


# --- .env resource mount threading ---


def _idle_events() -> list[BetaManagedAgentsSessionEvent]:
    """Minimal happy-path event stream: one message then terminal idle."""
    return [
        BetaManagedAgentsAgentMessageEvent(
            id="evt_msg_1",
            type="agent.message",
            processed_at=_NOW,
            content=[BetaManagedAgentsTextBlock(type="text", text="ok")],
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id="evt_idle_1",
            type="session.status_idle",
            processed_at=_NOW,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    ]


def _build_client_with_files(
    events: list[BetaManagedAgentsSessionEvent],
    *,
    file_id: str,
    session_create_bodies: list[dict[str, Any]],
) -> AsyncAnthropic:
    """Transport client serving Files upload + session create/stream/send.

    Captures POST /v1/sessions request bodies for resources/vault assertion.
    """
    router = MARouter()
    session_json = _fake_session_json()
    event_dicts = [e.model_dump(mode="json") for e in events]

    def handle_files(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json=FileMetadata(
                id=file_id,
                created_at=_NOW,
                filename=".env",
                mime_type="text/plain",
                size_bytes=len(request.content),
                type="file",
            ).model_dump(mode="json"),
        )

    def handle_create(request: httpx.Request, match: Any) -> httpx.Response:
        session_create_bodies.append(json.loads(request.content))
        return httpx.Response(200, json=session_json)

    def handle_stream(request: httpx.Request, match: Any) -> httpx.Response:
        return sse_response(event_dicts)

    def handle_send(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(200, json={"data": None})

    router.add("POST", r"/v1/files", handle_files)
    router.add("POST", r"/v1/sessions", handle_create)
    router.add("GET", r"/v1/sessions/[^/]+/events/stream", handle_stream)
    router.add("POST", r"/v1/sessions/[^/]+/events", handle_send)
    _register_agent_environment_routes(router)
    return build_fake_anthropic(router.dispatch)


async def test_run_turn_mounts_env_resource_when_agent_has_secrets(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    await put_agent_file(
        db_session, tenant_id=tenant.id, agent_id=agent_uuid, key="API_KEY", content="secret"
    )
    await db_session.commit()

    bodies: list[dict[str, Any]] = []
    client = _build_client_with_files(
        _idle_events(), file_id="file_env_hr", session_create_bodies=bodies
    )

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
    )

    assert tail == "ok", "turn still drains to idle and returns its tail"
    assert len(bodies) == 1, "exactly one session-create call"
    assert bodies[0].get("resources") == [
        {"type": "file", "file_id": "file_env_hr", "mount_path": ".env"}
    ], "session-create must carry the .env resource when the agent has secrets"


async def test_run_turn_mounts_repo_resource_when_bound_and_pat_present(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Headless parity with create_session: a bound repo + PAT → clone resource."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await repo_binding_store.set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        repo_url="https://github.com/example-org/example-repo",
        default_branch="main",
        ma_secret_ref="anon:",
    )
    await github_binding_store.set_agent_github_binding(
        db_session, agent_id=agent_uuid, principal_id=agent_uuid
    )
    await db_session.commit()
    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=agent_uuid,
        github_login="dev-agent",
        plaintext_token="ghp_dev_agent_token",
        scopes=("repo",),
    )

    bodies: list[dict[str, Any]] = []
    client = _build_client_with_files(
        _idle_events(), file_id="file_unused", session_create_bodies=bodies
    )

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        fernet=fernet,
    )

    assert tail == "ok"
    assert len(bodies) == 1, "exactly one session-create call"
    assert bodies[0].get("resources") == [
        {
            "type": "github_repository",
            "url": "https://github.com/example-org/example-repo",
            "authorization_token": "ghp_dev_agent_token",
            "checkout": {"type": "branch", "name": "main"},
        }
    ], "headless session-create must carry the github_repository clone resource"


async def test_run_turn_provisions_copilot_credential_from_pat(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Headless parity: a bound repo + PAT + ensured vault → Copilot cred on the vault."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    account_id = uuid.uuid4()
    public_url = "https://mcp.example.local/mcp"
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await repo_binding_store.set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        repo_url="https://github.com/example-org/example-repo",
        default_branch="main",
        ma_secret_ref="anon:",
    )
    await github_binding_store.set_agent_github_binding(
        db_session, agent_id=agent_uuid, principal_id=agent_uuid
    )
    await db_session.commit()
    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=agent_uuid,
        github_login="dev-agent",
        plaintext_token="ghp_copilot_pat",
        scopes=("repo",),
    )

    cred_bodies: list[dict[str, Any]] = []
    session_bodies: list[dict[str, Any]] = []
    router = MARouter()
    session_json = _fake_session_json()
    event_dicts = [e.model_dump(mode="json") for e in _idle_events()]
    display = f"daimon-mcp:{account_id}:{agent_uuid}"

    def handle_vaults_list(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "vlt_existing",
                        "type": "vault",
                        "display_name": display,
                        "metadata": None,
                        "archived_at": None,
                        "created_at": "2026-04-01T00:00:00Z",
                    }
                ],
                "has_more": False,
            },
        )

    def handle_creds_list(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "vcrd_daimon_mcp",
                        "type": "credential",
                        "vault_id": "vlt_existing",
                        "auth": {"type": "static_bearer", "mcp_server_url": public_url},
                    }
                ],
                "has_more": False,
            },
        )

    def handle_creds_create(request: httpx.Request, match: Any) -> httpx.Response:
        cred_bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "vcrd_copilot",
                "type": "credential",
                "vault_id": "vlt_existing",
                "auth": {
                    "type": "static_bearer",
                    "mcp_server_url": "https://api.githubcopilot.com/mcp",
                },
            },
        )

    def handle_session_create(request: httpx.Request, match: Any) -> httpx.Response:
        session_bodies.append(json.loads(request.content))
        return httpx.Response(200, json=session_json)

    def handle_stream(request: httpx.Request, match: Any) -> httpx.Response:
        return sse_response(event_dicts)

    def handle_send(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(200, json={"data": None})

    router.add("GET", r"/v1/vaults", handle_vaults_list)
    router.add("GET", r"/v1/vaults/[^/]+/credentials", handle_creds_list)
    router.add("POST", r"/v1/vaults/[^/]+/credentials", handle_creds_create)
    router.add("POST", r"/v1/sessions", handle_session_create)
    router.add("GET", r"/v1/sessions/[^/]+/events/stream", handle_stream)
    router.add("POST", r"/v1/sessions/[^/]+/events", handle_send)
    _register_agent_environment_routes(router)
    client = build_fake_anthropic(router.dispatch)

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        mcp_settings=McpSettings(jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(public_url)),
        account_id=account_id,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        fernet=fernet,
    )

    assert tail == "ok"
    assert len(cred_bodies) == 1, "exactly one credential POST (the Copilot cred)"
    auth = cred_bodies[0]["auth"]
    assert auth["mcp_server_url"] == "https://api.githubcopilot.com/mcp"
    assert auth["token"] == "ghp_copilot_pat"
    assert session_bodies[0].get("vault_ids") == ["vlt_existing"]


async def test_run_turn_omits_resources_when_phase51_params_absent() -> None:
    """Backward compat: without tenant/agent/factory, no resources, turn returns tail."""
    bodies: list[dict[str, Any]] = []
    client = _build_client_with_files(
        _idle_events(), file_id="file_unused", session_create_bodies=bodies
    )

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
    )

    assert tail == "ok", "turn drains normally without resource-mount params"
    assert len(bodies) == 1, "exactly one session-create call"
    assert "resources" not in bodies[0], "no resources when resource-mount params are absent"


async def test_run_turn_composes_resources_alongside_vault_ids(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """resources must compose with the vault_ids branch, not replace it."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    account_id = uuid.uuid4()
    public_url = "https://mcp.example.local/mcp"
    await put_agent_file(
        db_session, tenant_id=tenant.id, agent_id=agent_uuid, key="API_KEY", content="secret"
    )
    await db_session.commit()

    bodies: list[dict[str, Any]] = []
    session_json = _fake_session_json()
    event_dicts = [e.model_dump(mode="json") for e in _idle_events()]
    router = MARouter()

    def handle_vaults_list(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "has_more": False})

    def handle_vaults_create(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "vlt_new",
                "type": "vault",
                "display_name": f"daimon-mcp:{account_id}:{agent_uuid}",
                "metadata": None,
                "archived_at": None,
                "created_at": "2026-05-20T00:00:00Z",
            },
        )

    def handle_credentials_create(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "vcrd_new",
                "type": "vault_credential",
                "vault_id": "vlt_new",
                "metadata": {},
                "created_at": "2026-05-20T00:00:00Z",
                "updated_at": "2026-05-20T00:00:00Z",
                "auth": {"type": "static_bearer", "mcp_server_url": public_url},
            },
        )

    def handle_files(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json=FileMetadata(
                id="file_both_hr",
                created_at=_NOW,
                filename=".env",
                mime_type="text/plain",
                size_bytes=len(request.content),
                type="file",
            ).model_dump(mode="json"),
        )

    def handle_create(request: httpx.Request, match: Any) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=session_json)

    def handle_stream(request: httpx.Request, match: Any) -> httpx.Response:
        return sse_response(event_dicts)

    def handle_send(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(200, json={"data": None})

    router.add("GET", r"/v1/vaults", handle_vaults_list)
    router.add("POST", r"/v1/vaults", handle_vaults_create)
    router.add("POST", r"/v1/vaults/[^/]+/credentials", handle_credentials_create)
    router.add("POST", r"/v1/files", handle_files)
    router.add("POST", r"/v1/sessions", handle_create)
    router.add("GET", r"/v1/sessions/[^/]+/events/stream", handle_stream)
    router.add("POST", r"/v1/sessions/[^/]+/events", handle_send)
    _register_agent_environment_routes(router)
    client = build_fake_anthropic(router.dispatch)

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        mcp_settings=McpSettings(
            jwt_secret=SecretStr("x" * 32),
            public_url=HttpUrl(public_url),
        ),
        account_id=account_id,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
    )

    assert tail == "ok"
    assert len(bodies) == 1, "exactly one session-create call"
    assert bodies[0].get("vault_ids") == ["vlt_new"], (
        "vault_ids behavior must be preserved alongside resources"
    )
    assert bodies[0].get("resources") == [
        {"type": "file", "file_id": "file_both_hr", "mount_path": ".env"}
    ], "resources must compose alongside vault_ids, not replace it"
