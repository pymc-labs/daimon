"""Tests for daimon.core.headless_runner.run_turn.

Non-interactive turn driver — opens an MA session, sends a single trigger
message, drains SSE through the turn-state reducers until terminal `status_idle`,
returns the complete final message.

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

import asyncio
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
    FileMetadata,
)
from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMCPToolUseEvent,
    BetaManagedAgentsSessionEvent,
)
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
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from anthropic.types.beta.sessions.beta_managed_agents_unknown_error import (
    BetaManagedAgentsUnknownError,
)
from daimon.core import headless_runner
from daimon.core.config import McpSettings
from daimon.core.defaults.metadata import MA_METADATA_KEY_BILLING_EXEMPT
from daimon.core.errors import TurnError
from daimon.core.rule_views import RoutineOrigin
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.errors import ScopeViolation
from mux.testing.drivers.anthropic.ma import (
    MARouter,
    build_fake_anthropic,
    list_response,
    session_response,
    sse_response,
)
from mux.testing.drivers.anthropic.ma_models import ma_model_usage, ma_session
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .turn_path_support import HeadlessIdentity, TurnIOSpy, TurnPath

_NOW = dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=dt.UTC)
_SESSION_ID = "ses_test"
_MODEL_ID = "claude-sonnet-4-5"

RunTurn = Callable[..., Awaitable[str]]


@pytest.fixture
def run_turn(
    turn_path: TurnPath, headless_identity: HeadlessIdentity, turn_io_used: TurnIOSpy
) -> RunTurn:
    """`headless_runner.run_turn` on this leg's turn path, for a real tenant and account.

    A test overrides any of the three by passing it. Every turn it drives must
    have used the leg's own turn I/O: a turn that never reached the driver
    (a refusal, a deadline breach during assembly) is held only to not having
    used the other leg's I/O.
    """

    async def bound(**kwargs: Any) -> str:
        kwargs.setdefault("turn_path", turn_path)
        kwargs.setdefault("tenant_id", headless_identity.tenant_id)
        kwargs.setdefault("account_id", headless_identity.account_id)
        kwargs.setdefault("tool_safety", OPEN_TOOL_SAFETY)
        try:
            tail = await headless_runner.run_turn(**kwargs)
        except BaseException:
            turn_io_used.assert_matches(turn_path)
            raise
        turn_io_used.assert_ran(turn_path)
        return tail

    return bound


def _fake_session_json(session_id: str = _SESSION_ID, model_id: str = _MODEL_ID) -> dict[str, Any]:
    """Build a real BetaManagedAgentsSession payload for POST /v1/sessions responses."""
    return ma_session(
        id=session_id, agent_id="agent_x", model=model_id, environment_id="env_x", created_at=_NOW
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
        agent = ma_agent(id=match.group(1), name="test-agent", model=model_id, created_at=_NOW)
        return httpx.Response(200, json=agent.model_dump(mode="json"))

    def handle_environment_retrieve(request: httpx.Request, match: Any) -> httpx.Response:
        environment = ma_environment(
            id=match.group(1), name="test-env", created_at=_NOW.isoformat()
        )
        return httpx.Response(200, json=environment.model_dump(mode="json"))

    router.add("GET", r"/v1/agents/([^/]+)", handle_agent_retrieve)
    router.add("GET", r"/v1/environments/([^/]+)", handle_environment_retrieve)


def _register_session_vault_routes(router: MARouter) -> None:
    """Serve the App session vault used by turns with stored resources."""
    router.add(
        "GET",
        r"/v1/vaults",
        lambda _r, _m: httpx.Response(200, json={"data": [], "has_more": False}),
    )
    router.add(
        "POST",
        r"/v1/vaults$",
        lambda _r, _m: httpx.Response(
            200,
            json={
                "id": "vlt_session",
                "type": "vault",
                "display_name": "github-session:test",
                "metadata": None,
                "archived_at": None,
                "created_at": "2026-05-20T00:00:00Z",
            },
        ),
    )
    router.add(
        "POST",
        r"/v1/vaults/[^/]+/archive",
        lambda _r, _m: httpx.Response(200, json={"id": "vlt_session", "type": "vault"}),
    )
    router.add(
        "GET",
        r"/v1/vaults/[^/]+/credentials",
        lambda _r, _m: httpx.Response(200, json={"data": [], "has_more": False}),
    )


def _build_client(
    events: list[BetaManagedAgentsSessionEvent],
    *,
    session_id: str = _SESSION_ID,
    model_id: str = _MODEL_ID,
    send_capture: list[dict[str, Any]] | None = None,
    session_create_capture: list[dict[str, Any]] | None = None,
    terminal_less_script: bool = False,
) -> AsyncAnthropic:
    """Build transport-level client with session create + stream + send handlers.

    If `send_capture` is provided (a list), all POST /v1/sessions/{id}/events
    request bodies are appended to it for assertion. If `session_create_capture`
    is provided, the POST /v1/sessions request body is appended to it.

    `terminal_less_script=True` additionally registers `GET /v1/sessions/{id}`
    (status, "idle" -- no reopen) and `GET /v1/sessions/{id}/events` (replay,
    the same `events`) -- required whenever `events` does not end in a real
    terminal event. After plan 19-02 the driver checks MA's session status on
    ANY clean close or read timeout (including one caused by a `session.error`
    event, which the reducer folds into `TurnState.error` but which is NOT
    itself stream-terminal to the driver's consume loop -- only
    `session.status_idle`/`session.status_terminated` are), and finalizes via
    a replay-and-refold rather than reopening the stream.
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
    _register_session_vault_routes(router)
    if terminal_less_script:
        router.add(
            "GET",
            r"/v1/sessions/[^/]+$",
            lambda request, match: session_response(session_id=session_id, status="idle"),
        )
        router.add(
            "GET",
            r"/v1/sessions/[^/]+/events",
            lambda request, match: list_response(event_dicts),
        )
    # The unattended gate reads the session before each send.
    router.add("GET", r"/v1/sessions/[^/]+$", lambda _r, _m: httpx.Response(200, json=session_json))
    _register_agent_environment_routes(router, model_id=model_id)
    return build_fake_anthropic(router.dispatch)


@pytest.mark.parametrize("origin", ["chat", "routine", "relay", "handoff"])
async def test_run_turn_returns_last_message_text(
    origin, db_session, db_session_factory, run_turn: RunTurn, headless_identity: HeadlessIdentity
) -> None:

    tenant_id = headless_identity.tenant_id
    await db_session.commit()
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
    sent = []
    client = _build_client(events, send_capture=sent)

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        origin=origin,
        tenant_id=tenant_id,
        session_factory=db_session_factory,
        tool_safety=OPEN_TOOL_SAFETY,
    )

    assert tail == "hello world", "run_turn should return the agent.message text"
    from daimon.core.context_prompt import context_prompt

    assert sent[0]["events"][0]["content"][0]["text"] == context_prompt(origin) + "hi"

    from daimon.core.stores.turn_outcomes import list_for_tenant
    from daimon.core.turn.outcomes import drain_outcomes

    await drain_outcomes()
    async with db_session_factory() as session:
        outcomes = await list_for_tenant(session, tenant_id)
    assert len(outcomes) == 1
    assert outcomes[0].origin == origin
    assert outcomes[0].agent_id == "agent_x"
    assert outcomes[0].reason == "completed"
    assert "hello world" not in str(outcomes[0])


async def test_full_final_response_is_returned_for_delivery(run_turn: RunTurn) -> None:
    long_text = "HEAD-ONLY " + "completeword " * 300 + "END-COMPLETE"
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
        tool_safety=OPEN_TOOL_SAFETY,
    )

    assert tail == long_text, "the scheduler needs the complete reply, including its ending"


async def test_auto_allow_confirms_once_and_rides_out_a_reemitted_requires_action(
    run_turn: RunTurn,
) -> None:
    """Two requires_action idle events naming the same blocked id: the first
    is allowed (tool safety off) exactly once (no double-ack). MA repeats the pause while
    the batch's other tools run, so a repeat read before MA has taken the
    allow is a duplicate, not a re-ask: the turn carries on to its answer.
    Production 2026-10-08 ended such turns as `requires_action`. A real
    re-ask after MA took the allow still ends the turn (`test_driver_auto_approve`).
    """
    send_capture: list[dict[str, Any]] = []

    events: list[BetaManagedAgentsSessionEvent] = [
        # The blocked call itself: an unattended decider refuses a pause it
        # has no tool-use evidence for, whatever the policy.
        BetaManagedAgentsAgentMCPToolUseEvent(
            id="tu_x",
            type="agent.mcp_tool_use",
            processed_at=_NOW,
            mcp_server_name="linear",
            name="create_issue",
            input={},
            evaluated_permission="ask",
        ),
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

    await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        tool_safety=OPEN_TOOL_SAFETY,
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


async def test_session_error_raises_turn_error(run_turn: RunTurn) -> None:
    """A `session.error` event is not itself stream-terminal to the driver's
    consume loop (only `session.status_idle`/`session.status_terminated`
    are), so the stream ending right after it is a clean close: the driver
    checks MA's session status, finalizes via replay-and-refold (no reopen,
    the status is `idle`), and the replayed `session.error` folds into
    `TurnState.error` -- a `TurnError(kind="upstream")`, raised as-is,
    replacing the old bespoke `RuntimeError("session.error: ...")`.
    """
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
    client = _build_client(events, terminal_less_script=True)

    with pytest.raises(TurnError) as exc_info:
        await run_turn(
            anthropic=client,
            agent_id="agent_x",
            environment_id="env_x",
            trigger_message="hi",
            tool_safety=OPEN_TOOL_SAFETY,
        )

    assert exc_info.value.kind == "upstream"
    assert "boom" in exc_info.value.message


async def test_run_turn_calls_usage_record_for_span_model_request_end(run_turn: RunTurn) -> None:
    from unittest.mock import AsyncMock

    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsSpanModelRequestEndEvent(
            id="evt_span_1",
            type="span.model_request_end",
            processed_at=_NOW,
            model_request_start_id="evt_span_start_1",
            model_usage=ma_model_usage(input_tokens=10, output_tokens=5),
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
        tool_safety=OPEN_TOOL_SAFETY,
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
    assert "session_id" not in call.kwargs, (
        "record is invoked event-only; session identity is pre-bound by the factory"
    )
    assert call.kwargs["event"].id == "evt_span_1"


@pytest.mark.parametrize("model_id", [_MODEL_ID, "claude-sonnet-4-6"])
async def test_run_turn_without_usage_record_factory_completes_unbilled(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    model_id: str,
    run_turn: RunTurn,
    headless_identity: HeadlessIdentity,
) -> None:
    """No usage_record_factory -> BillingExempt(reason="headless-unrecorded"):
    the turn still completes and returns its tail, and no span event is ever
    metered (there is no recorder to invoke)."""
    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsSpanModelRequestEndEvent(
            id="evt_span_1",
            type="span.model_request_end",
            processed_at=_NOW,
            model_request_start_id="evt_span_start_1",
            model_usage=ma_model_usage(input_tokens=10, output_tokens=5),
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
    tenant_id = headless_identity.tenant_id
    await db_session.commit()
    sm = async_sessionmaker(db_engine)
    client = _build_client(events, model_id=model_id)

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        tenant_id=tenant_id,
        session_factory=sm,
        # usage_record_factory intentionally omitted,
        tool_safety=OPEN_TOOL_SAFETY,
    )

    assert tail == "ok", "an unbilled turn still drains normally and returns its tail"

    from daimon.core.stores.turn_outcomes import list_for_tenant
    from daimon.core.turn.outcomes import drain_outcomes

    await drain_outcomes()
    async with sm() as session:
        rows = await list_for_tenant(session, tenant_id)
    assert len(rows) == 1
    assert rows[0].billing_posture == "exempt"
    assert rows[0].model_calls == 1
    assert rows[0].input_tokens == 10 and rows[0].output_tokens == 5
    assert rows[0].model_ids == [model_id]
    if model_id == _MODEL_ID:  # This fixture uses a model absent from the price table.
        assert rows[0].cost_usd is None and rows[0].unpriced_calls == 1
    else:
        assert rows[0].cost_usd is not None and rows[0].cost_usd > 0
        assert rows[0].unpriced_calls == 0


async def test_run_turn_reconnects_through_a_clean_close_and_returns_post_reconnect_tail(
    run_turn: RunTurn,
) -> None:
    """The Class A fix reaching routines: a clean SSE close with the session
    still `running` triggers a status-checked reconnect (not a quiet,
    truncated finalize as a false success), and the returned tail reflects
    the content delivered only after the reconnect."""
    stream_calls = 0

    def handle_stream(request: httpx.Request, match: Any) -> httpx.Response:
        nonlocal stream_calls
        stream_calls += 1
        if stream_calls == 1:
            # Clean close: the connection ends with no events at all, no
            # terminal event -- the pre-19-09 bespoke drain would have
            # treated this stream end as a (falsely) completed turn.
            return sse_response([])
        return sse_response(
            [
                BetaManagedAgentsAgentMessageEvent(
                    id="evt_msg_post_reconnect",
                    type="agent.message",
                    processed_at=_NOW,
                    content=[BetaManagedAgentsTextBlock(type="text", text="post-reconnect")],
                ).model_dump(mode="json"),
                BetaManagedAgentsSessionStatusIdleEvent(
                    id="evt_idle_post_reconnect",
                    type="session.status_idle",
                    processed_at=_NOW,
                    stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
                ).model_dump(mode="json"),
            ]
        )

    def handle_status(request: httpx.Request, match: Any) -> httpx.Response:
        return session_response(session_id=_SESSION_ID, status="running")

    def handle_events_list(request: httpx.Request, match: Any) -> httpx.Response:
        # Nothing was folded from the first (empty) stream, so replay is empty.
        return list_response([])

    router = MARouter()
    router.add(
        "POST",
        r"/v1/sessions",
        lambda request, match: httpx.Response(200, json=_fake_session_json()),
    )
    router.add("GET", r"/v1/sessions/[^/]+/events/stream", handle_stream)
    router.add("GET", r"/v1/sessions/[^/]+/events", handle_events_list)
    router.add(
        "POST",
        r"/v1/sessions/[^/]+/events",
        lambda request, match: httpx.Response(200, json={"data": None}),
    )
    router.add("GET", r"/v1/sessions/[^/]+$", handle_status)
    _register_agent_environment_routes(router)
    client = build_fake_anthropic(router.dispatch)

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        tool_safety=OPEN_TOOL_SAFETY,
    )

    assert stream_calls == 2, "the stream must reopen exactly once after the clean close"
    assert tail == "post-reconnect", (
        "the tail must reflect the event delivered after the reconnect, not a quietly "
        "truncated pre-close state"
    )


async def test_run_turn_propagates_usage_record_factory_exception(run_turn: RunTurn) -> None:
    """If usage_record raises (fail-closed), run_turn lets it propagate."""
    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsSpanModelRequestEndEvent(
            id="evt_span_boom",
            type="span.model_request_end",
            processed_at=_NOW,
            model_request_start_id="evt_span_start_1",
            model_usage=ma_model_usage(input_tokens=10, output_tokens=5),
        ),
    ]
    client = _build_client(events)

    async def boom(*, event: object) -> None:
        del event
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
            tool_safety=OPEN_TOOL_SAFETY,
        )


async def test_run_turn_propagates_httpx_error(run_turn: RunTurn) -> None:
    """A persistent transport error from events.stream exhausts the driver's
    bounded 2-attempt connection-error retry budget and surfaces as a
    TurnError(kind="connection_lost") with the original anthropic.APIConnectionError
    preserved as `cause` -- replacing the old bespoke behavior of the raw SDK
    error propagating directly (the driver folds it into TurnState.error
    instead of raising it, per its own finalizer contract)."""
    router = MARouter()

    def handle_create(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(200, json=_fake_session_json())

    def handle_send(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(200, json={"data": None})

    def handle_stream(request: httpx.Request, match: Any) -> httpx.Response:
        raise httpx.ConnectError("boom")

    router.add("POST", r"/v1/sessions", handle_create)
    router.add(
        "GET", r"/v1/sessions/[^/]+$", lambda _r, _m: httpx.Response(200, json=_fake_session_json())
    )
    router.add("POST", r"/v1/sessions/[^/]+/events", handle_send)
    router.add("GET", r"/v1/sessions/[^/]+/events/stream", handle_stream)
    _register_agent_environment_routes(router)
    client = build_fake_anthropic(router.dispatch)

    with pytest.raises(TurnError) as exc_info:
        await run_turn(
            anthropic=client,
            agent_id="agent_x",
            environment_id="env_x",
            trigger_message="hi",
            tool_safety=OPEN_TOOL_SAFETY,
        )

    assert exc_info.value.kind == "connection_lost"
    assert isinstance(exc_info.value.cause, anthropic.APIConnectionError), (
        "the original SDK connection error must be preserved as the TurnError's cause"
    )


async def test_run_turn_attaches_vault_when_mcp_settings(
    db_session_factory: async_sessionmaker[AsyncSession],
    run_turn: RunTurn,
    headless_identity: HeadlessIdentity,
) -> None:
    """Cold-path: ensure_agent_mcp_vault performs vaults.list → vaults.create, then
    run_turn passes the new per-agent vault id into beta.sessions.create as vault_ids.
    """
    account_id = headless_identity.account_id
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
    router.add(
        "GET",
        r"/v1/vaults/[^/]+/credentials",
        lambda _r, _m: httpx.Response(200, json={"data": [], "has_more": False}),
    )
    router.add("POST", r"/v1/vaults/[^/]+/credentials", handle_credentials_create)
    router.add(
        "POST",
        r"/v1/vaults/[^/]+/archive",
        lambda _r, _m: httpx.Response(200, json={"id": "vlt_new", "type": "vault"}),
    )
    router.add("POST", r"/v1/sessions", handle_session_create)
    router.add(
        "GET", r"/v1/sessions/[^/]+$", lambda _r, _m: httpx.Response(200, json=_fake_session_json())
    )
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
        session_factory=db_session_factory,
        tool_safety=OPEN_TOOL_SAFETY,
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


async def test_run_turn_raises_value_error_when_account_id_missing(
    run_turn: RunTurn, turn_path: TurnPath
) -> None:
    """When mcp_settings carries both fields but account_id is None,
    run_turn must raise ValueError with the exact wording from sessions.py.

    On the mux path the turn is refused earlier still: it never runs without an
    authorized account (`headless_runner.run_turn_impl`), so the sessions.py guard
    is not reached.
    """
    # The agent/environment retrieve bridge runs before create_session's
    # ValueError guard, so those two routes must exist even though no
    # session-create/events call is expected.
    router = MARouter()
    _register_agent_environment_routes(router)
    client = build_fake_anthropic(router.dispatch)

    refusal: tuple[type[Exception], str] = (
        (ValueError, r"^account_id is required when mcp_settings has public_url and jwt_secret$")
        if turn_path == "legacy"
        else (ScopeViolation, r"mux headless turns require the authorized tenant/account")
    )
    with pytest.raises(refusal[0], match=refusal[1]):
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
            tool_safety=OPEN_TOOL_SAFETY,
        )


@pytest.mark.parametrize(
    "identity",
    ["no tenant", "no account", "neither"],
)
async def test_a_headless_turn_without_its_identity_runs_on_legacy_and_is_refused_on_mux(
    identity: str, run_turn: RunTurn, turn_path: TurnPath, turn_io_used: TurnIOSpy
) -> None:
    """The mux refusal for an unauthorized turn stays: no tenant or no account
    means no session is opened and no turn I/O is built. Legacy keeps running
    such a turn (a tenantless caller is the legacy host)."""
    session_create_capture: list[dict[str, Any]] = []
    client = _build_client(_idle_events(), session_create_capture=session_create_capture)
    missing = {
        "no tenant": {"tenant_id": None},
        "no account": {"account_id": None},
        "neither": {"tenant_id": None, "account_id": None},
    }[identity]

    async def attempt() -> str:
        return await run_turn(
            anthropic=client,
            agent_id="agent_x",
            environment_id="env_x",
            trigger_message="hi",
            **missing,
        )

    if turn_path == "mux":
        with pytest.raises(ScopeViolation, match="require the authorized tenant/account"):
            await attempt()
        assert session_create_capture == [], "a refused turn must not open a session"
        assert turn_io_used.classes == [], "a refused turn must not build turn I/O"
    else:
        assert await attempt() == "ok"
        assert len(session_create_capture) == 1
        turn_io_used.assert_ran("legacy")


async def test_the_turn_path_argument_wins_over_settings(
    headless_identity: HeadlessIdentity,
    turn_path: TurnPath,
    turn_io_used: TurnIOSpy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`turn_path` is read once at the edge and handed down: with settings
    pointing at the other path, the turn still runs on the one it was given."""
    other: TurnPath = "mux" if turn_path == "legacy" else "legacy"
    monkeypatch.setenv("DAIMON_TURN__PATH", other)
    client = _build_client(_idle_events())

    tail = await headless_runner.run_turn(
        tool_safety=OPEN_TOOL_SAFETY,
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        tenant_id=headless_identity.tenant_id,
        account_id=headless_identity.account_id,
        turn_path=turn_path,
    )

    assert tail == "ok"
    turn_io_used.assert_ran(turn_path)


async def test_run_turn_raises_when_mcp_active_and_agent_uuid_none(run_turn: RunTurn) -> None:
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
            tool_safety=OPEN_TOOL_SAFETY,
        )


async def test_run_turn_stamps_session_metadata_with_tenant_and_account(run_turn: RunTurn) -> None:
    """A scheduler-fired (headless) session must carry
    daimon_tenant + daimon_account metadata, mirroring create_session's stamp
    (test_sessions.py::test_create_session_tags_metadata_with_account_and_tenant_when_both_provided),
    so usage_sweep.session_billing does not skip it and routine sessions get
    billed.
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
    from unittest.mock import AsyncMock

    def factory(session_id: str, model_id: str) -> AsyncMock:
        return AsyncMock(return_value=None)

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        tenant_id=tenant_id,
        account_id=account_id,
        usage_record_factory=factory,  # the scheduler always passes one,
        tool_safety=OPEN_TOOL_SAFETY,
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
    assert MA_METADATA_KEY_BILLING_EXEMPT not in metadata, (
        "a recorded (Billed) routine session must stay sweepable"
    )


async def test_run_turn_without_usage_record_factory_stamps_session_billing_exempt(
    run_turn: RunTurn,
) -> None:
    """An unrecorded headless run is BillingExempt("headless-unrecorded"); its
    session carries daimon_billing_exempt so sweep_headless_usage skips it
    instead of debiting the tenant."""
    session_create_capture: list[dict[str, Any]] = []
    client = _build_client(_idle_events(), session_create_capture=session_create_capture)

    await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        tenant_id=uuid.uuid4(),
        tool_safety=OPEN_TOOL_SAFETY,
    )

    assert len(session_create_capture) == 1, "exactly one session-create call"
    metadata = session_create_capture[0]["metadata"]
    assert metadata[MA_METADATA_KEY_BILLING_EXEMPT] == "headless-unrecorded", (
        f"an unrecorded headless session must be stamped exempt; got {metadata!r}"
    )


@pytest.mark.parametrize("observed", [False, True], ids=["unobserved", "observed"])
async def test_run_turn_stamps_a_routine_session_with_where_it_fires(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    observed: bool,
    run_turn: RunTurn,
    headless_identity: HeadlessIdentity,
) -> None:
    """A routine session carries its destination's channel, thread and seal, so
    its transcript reads only where a turn there would, whether or not the run is
    observed (the scheduler's runs are: they pass a session factory)."""

    tenant_id = headless_identity.tenant_id
    await db_session.commit()
    session_create_capture: list[dict[str, Any]] = []
    client = _build_client(_idle_events(), session_create_capture=session_create_capture)

    await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        tenant_id=tenant_id,
        session_factory=db_session_factory if observed else None,
        origin_place=RoutineOrigin(
            channel_id="c1", thread_id="t1", seal_ids=frozenset({"c1"}), private_dm_id="routine:r1"
        ),
        tool_safety=OPEN_TOOL_SAFETY,
    )

    metadata = session_create_capture[0]["metadata"]
    assert (metadata.get("daimon_channel"), metadata.get("daimon_thread")) == ("c1", "t1"), (
        f"the routine session must be stamped with its destination; got {metadata!r}"
    )
    assert metadata.get("daimon_sealed") == "c1", "and with the seal over it"
    assert metadata.get("daimon_private_dm") == "routine:r1", "and kept private to its owner"


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
    router.add("GET", r"/v1/sessions/[^/]+$", lambda _r, _m: httpx.Response(200, json=session_json))
    _register_session_vault_routes(router)
    _register_agent_environment_routes(router)
    return build_fake_anthropic(router.dispatch)


async def test_run_turn_mounts_env_resource_when_agent_has_secrets(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    run_turn: RunTurn,
    headless_identity: HeadlessIdentity,
) -> None:
    tenant_id = headless_identity.tenant_id
    agent_uuid = uuid.uuid4()
    await put_agent_file(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_uuid,
        key="API_KEY",
        content="secret",
        set_by_account_id=None,
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
        tenant_id=tenant_id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        tool_safety=OPEN_TOOL_SAFETY,
    )

    assert tail == "ok", "turn still drains to idle and returns its tail"
    assert len(bodies) == 1, "exactly one session-create call"
    assert bodies[0].get("resources") == [
        {"type": "file", "file_id": "file_env_hr", "mount_path": ".env"}
    ], "session-create must carry the .env resource when the agent has secrets"


async def test_run_turn_omits_resources_when_phase51_params_absent(run_turn: RunTurn) -> None:
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
        tool_safety=OPEN_TOOL_SAFETY,
    )

    assert tail == "ok", "turn drains normally without resource-mount params"
    assert len(bodies) == 1, "exactly one session-create call"
    assert "resources" not in bodies[0], "no resources when resource-mount params are absent"


async def test_run_turn_composes_resources_alongside_vault_ids(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    run_turn: RunTurn,
    headless_identity: HeadlessIdentity,
) -> None:
    """resources must compose with the vault_ids branch, not replace it."""
    tenant_id = headless_identity.tenant_id
    agent_uuid = uuid.uuid4()
    account_id = headless_identity.account_id
    public_url = "https://mcp.example.local/mcp"
    await put_agent_file(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_uuid,
        key="API_KEY",
        content="secret",
        set_by_account_id=None,
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
    router.add(
        "GET",
        r"/v1/vaults/[^/]+/credentials",
        lambda _r, _m: httpx.Response(200, json={"data": [], "has_more": False}),
    )
    router.add("POST", r"/v1/vaults/[^/]+/credentials", handle_credentials_create)
    router.add(
        "POST",
        r"/v1/vaults/[^/]+/archive",
        lambda _r, _m: httpx.Response(200, json={"id": "vlt_new", "type": "vault"}),
    )
    router.add("POST", r"/v1/files", handle_files)
    router.add("POST", r"/v1/sessions", handle_create)
    router.add(
        "GET", r"/v1/sessions/[^/]+$", lambda _r, _m: httpx.Response(200, json=_fake_session_json())
    )
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
        tenant_id=tenant_id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        tool_safety=OPEN_TOOL_SAFETY,
    )

    assert tail == "ok"
    assert len(bodies) == 1, "exactly one session-create call"
    assert bodies[0].get("vault_ids") == ["vlt_new"], (
        "the MCP caller vault must be mounted alongside resources without an App grant"
    )
    assert bodies[0].get("resources") == [
        {"type": "file", "file_id": "file_both_hr", "mount_path": ".env"}
    ], "resources must compose alongside vault_ids, not replace it"


# --- Ceiling coverage (19-12) ---


async def test_run_turn_past_deadline_raises_a_ceiling_turn_error_from_session_assembly(
    run_turn: RunTurn,
) -> None:
    """A deadline already in the past must breach during session assembly
    (agent/environment retrieve + create_session), before the drain ever
    starts -- proven by an empty session_create_capture.

    `remaining_s` clamps an already-past deadline to a 0.001s floor, but a
    fast in-process fake transport can complete the two retrieve calls
    within that floor before the real wall clock advances 1ms (observed:
    the breach lands in the drain instead). To make the assembly-phase
    breach deterministic, the agent-retrieve leg -- which runs BEFORE
    create_session in the code -- is slowed with a real `asyncio.sleep`,
    the same technique `test_run_prepared_turn.py`'s
    `_slow_stream_for_recovered_session` uses to breach a specific leg.
    """
    session_create_capture: list[dict[str, Any]] = []
    router = MARouter()
    session_json = _fake_session_json()

    def handle_create(request: httpx.Request, match: Any) -> httpx.Response:
        session_create_capture.append(json.loads(request.content))
        return httpx.Response(200, json=session_json)

    router.add("POST", r"/v1/sessions", handle_create)
    router.add(
        "GET", r"/v1/sessions/[^/]+$", lambda _r, _m: httpx.Response(200, json=_fake_session_json())
    )
    _register_agent_environment_routes(router)

    async def _slow_agent_retrieve(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/v1/agents/"):
            await asyncio.sleep(1.0)
        return router.dispatch(request)

    transport = httpx.MockTransport(_slow_agent_retrieve)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    client = anthropic.AsyncAnthropic(api_key="test", http_client=http_client)

    with pytest.raises(TurnError) as exc_info:
        await run_turn(
            anthropic=client,
            agent_id="agent_x",
            environment_id="env_x",
            trigger_message="hi",
            deadline=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5),
            tool_safety=OPEN_TOOL_SAFETY,
        )

    assert exc_info.value.kind == "ceiling", "a past deadline must breach as kind='ceiling'"
    assert session_create_capture == [], "a breach during assembly must never reach create_session"


async def test_run_turn_ceiling_breach_during_the_drain_raises_a_ceiling_turn_error(
    run_turn: RunTurn,
) -> None:
    """Assembly completes normally (proven by a captured session-create body);
    the breach happens once the drain's stream-open is deliberately slowed
    past a tight future deadline."""
    session_create_capture: list[dict[str, Any]] = []
    router = MARouter()
    session_json = _fake_session_json()

    def handle_create(request: httpx.Request, match: Any) -> httpx.Response:
        session_create_capture.append(json.loads(request.content))
        return httpx.Response(200, json=session_json)

    def handle_send(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(200, json={"data": None})

    router.add("POST", r"/v1/sessions", handle_create)
    router.add(
        "GET", r"/v1/sessions/[^/]+$", lambda _r, _m: httpx.Response(200, json=_fake_session_json())
    )
    router.add("POST", r"/v1/sessions/[^/]+/events", handle_send)
    _register_agent_environment_routes(router)

    async def _slow_stream(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/events/stream"):
            await asyncio.sleep(2.0)
        return router.dispatch(request)

    transport = httpx.MockTransport(_slow_stream)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    client = anthropic.AsyncAnthropic(api_key="test", http_client=http_client)

    with pytest.raises(TurnError) as exc_info:
        await run_turn(
            anthropic=client,
            agent_id="agent_x",
            environment_id="env_x",
            trigger_message="hi",
            deadline=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=0.3),
            tool_safety=OPEN_TOOL_SAFETY,
        )

    assert exc_info.value.kind == "ceiling", "a slow drain past the deadline must breach as ceiling"
    assert len(session_create_capture) == 1, (
        "assembly must have completed (session-create captured) before the drain breached"
    )


async def test_run_turn_omitting_the_deadline_still_completes_a_normal_turn(
    run_turn: RunTurn,
) -> None:
    """The fail-safe default (a full 45-minute window) must not interfere
    with an ordinary routine that never passes a deadline."""
    client = _build_client(_idle_events())

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        # deadline intentionally omitted,
        tool_safety=OPEN_TOOL_SAFETY,
    )

    assert tail == "ok", "omitting deadline must not prevent a normal routine from completing"


async def test_run_turn_deadline_feeds_remaining_s_rather_than_being_ignored(
    run_turn: RunTurn,
) -> None:
    """Cheap pin that `deadline` is actually plumbed into `remaining_s`,
    without waiting 45 minutes for the real default: a deadline of exactly
    `now` (ceiling_s=0.0) must breach the same way a past deadline does --
    whether the breach lands during assembly or the drain, the parameter is
    proven to be live rather than silently ignored."""
    from daimon.core.turn.ceiling import turn_deadline

    client = _build_client(_idle_events())

    with pytest.raises(TurnError) as exc_info:
        await run_turn(
            anthropic=client,
            agent_id="agent_x",
            environment_id="env_x",
            trigger_message="hi",
            deadline=turn_deadline(now=dt.datetime.now(dt.UTC), ceiling_s=0.0),
            tool_safety=OPEN_TOOL_SAFETY,
        )

    assert exc_info.value.kind == "ceiling", (
        "a deadline of exactly now must breach, proving the param feeds remaining_s"
    )


async def test_run_turn_creates_a_fresh_session_per_fire_and_never_reuses_one(
    run_turn: RunTurn,
) -> None:
    """Invariant pin: every routine fire gets its OWN MA session.

    `driver._events_since_last_turn_boundary` exempts a `requires_action` idle
    from the turn-boundary scan under `AutoApprove` — which is this path's
    posture — on the strength of this invariant. A `requires_action` idle CAN
    still end an AutoApprove turn (the already-confirmed / no-fresh-ids branch
    of `_finalize_success_or_error`), so if a routine ever ran two turns on one
    session, that ended turn's events would fold into the next fire's state.
    The exemption is only safe because the replay can never span two turns.

    `run_turn` takes no `session_id` and no caller supplies one; this pins the
    consequence rather than the wording, so it fails if that ever changes.
    """
    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsAgentMessageEvent(
            id="evt_msg_1",
            type="agent.message",
            processed_at=_NOW,
            content=[BetaManagedAgentsTextBlock(type="text", text="done")],
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id="evt_idle_1",
            type="session.status_idle",
            processed_at=_NOW,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    ]
    creates: list[dict[str, Any]] = []
    client = _build_client(events, session_create_capture=creates)

    for trigger in ("first fire", "second fire"):
        await run_turn(
            anthropic=client,
            agent_id="agent_x",
            environment_id="env_x",
            trigger_message=trigger,
            tool_safety=OPEN_TOOL_SAFETY,
        )

    assert len(creates) == 2, (
        "each routine fire must open its own MA session -- a reused session "
        "would let a prior turn's events reach the next fire's replay fold"
    )


@pytest.mark.parametrize("origin", ["chat", "routine", "relay", "handoff"])
async def test_memory_access_per_headless_origin(
    origin,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    run_turn: RunTurn,
    headless_identity: HeadlessIdentity,
) -> None:
    from daimon.core.stores.agent_memory_stores import insert_memory_store

    tenant_id = headless_identity.tenant_id
    agent_id = uuid.uuid4()
    await insert_memory_store(
        db_session, tenant_id=tenant_id, agent_id=agent_id, memory_store_id="memstore_origin"
    )
    await db_session.commit()
    bodies: list[dict[str, Any]] = []
    client = _build_client(_idle_events(), session_create_capture=bodies)
    await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        tenant_id=tenant_id,
        agent_uuid=agent_id,
        session_factory=db_session_factory,
        origin=origin,
        tool_safety=OPEN_TOOL_SAFETY,
    )
    memory = next(r for r in bodies[0]["resources"] if r["type"] == "memory_store")
    assert memory["access"] == ("read_only" if origin == "routine" else "read_write")


@pytest.mark.parametrize("reason", ["completed", "interrupted", "upstream", "ceiling"])
async def test_headless_result_callback_only_receives_completed_turns(reason):
    from daimon.core.headless_runner import _NoOpLifecycle
    from daimon.core.turn.state import TurnState
    from daimon.core.turn.termination import TerminationReason

    seen = []

    async def result(state):
        seen.append(state)

    lifecycle = _NoOpLifecycle(on_result=result)
    state = TurnState(termination=TerminationReason(reason))
    await lifecycle.on_terminal_success(state)
    assert seen == ([state] if reason == "completed" else [])


async def test_headless_routines_never_mount_owner_memory(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    run_turn: RunTurn,
) -> None:
    """The headless API cannot request personal memory, even for a known owner."""
    from daimon.core.stores.agent_memory_stores import (
        insert_memory_store,
        insert_personal_memory_store,
    )

    tenant = await make_tenant(db_session)
    owner = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_id, memory_store_id="memstore_shared"
    )
    await insert_personal_memory_store(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=owner.id,
        memory_store_id="memstore_owner",
    )
    await db_session.commit()
    bodies: list[dict[str, Any]] = []
    client = _build_client(_idle_events(), session_create_capture=bodies)
    await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        account_id=owner.id,
        tenant_id=tenant.id,
        agent_uuid=agent_id,
        session_factory=db_session_factory,
        origin="routine",
        tool_safety=OPEN_TOOL_SAFETY,
    )
    mounts = {
        r["memory_store_id"]: r["access"]
        for r in bodies[0]["resources"]
        if r["type"] == "memory_store"
    }
    assert mounts == {"memstore_shared": "read_only"}


async def test_a_held_routine_fire_reads_only_its_channels_store_read_only(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    run_turn: RunTurn,
    headless_identity: HeadlessIdentity,
) -> None:
    """A routine firing into a shared channel mounts that channel's store, read-only
    like every routine's memory, and neither the agent-wide nor the owner's store."""
    from daimon.core.stores.agent_memory_stores import (
        insert_channel_memory_store,
        insert_memory_store,
        insert_personal_memory_store,
    )
    from daimon.core.turn_hold import TurnHold

    tenant_id = headless_identity.tenant_id
    owner_id = headless_identity.account_id
    agent_id = uuid.uuid4()
    await insert_memory_store(
        db_session, tenant_id=tenant_id, agent_id=agent_id, memory_store_id="memstore_shared"
    )
    await insert_personal_memory_store(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=owner_id,
        memory_store_id="memstore_owner",
    )
    await insert_channel_memory_store(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        platform="slack",
        channel_id="C-SYN-1",
        memory_store_id="memstore_channel",
    )
    await db_session.commit()
    bodies: list[dict[str, Any]] = []
    client = _build_client(_idle_events(), session_create_capture=bodies)

    await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        account_id=owner_id,
        tenant_id=tenant_id,
        agent_uuid=agent_id,
        session_factory=db_session_factory,
        origin="routine",
        hold=TurnHold("slack", "C-SYN-1", client=False),
    )

    mounts = {
        r["memory_store_id"]: r["access"]
        for r in bodies[0]["resources"]
        if r["type"] == "memory_store"
    }
    assert mounts == {"memstore_channel": "read_only"}


async def test_headless_role_gated_target_is_refused_before_session_creation(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    run_turn: RunTurn,
    headless_identity: HeadlessIdentity,
) -> None:
    from daimon.core.access_policy import AgentRule, TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy
    from daimon.core.turn.errors import AdmissionDenied

    await set_access_policy(
        db_session,
        tenant_id=headless_identity.tenant_id,
        policy=TenantAccessPolicy(agent_rules={"test-agent": AgentRule(invoker_role_ids=("123",))}),
    )
    await db_session.commit()
    created: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []
    client = _build_client([], session_create_capture=created, send_capture=sent)
    try:
        with pytest.raises(AdmissionDenied, match="invoker_not_allowed"):
            await run_turn(
                anthropic=client,
                agent_id="agent_x",
                environment_id="env_x",
                trigger_message="work",
                tenant_id=headless_identity.tenant_id,
                session_factory=db_session_factory,
            )
    finally:
        await client.close()
    assert created == []
    assert sent == []


async def test_routine_receives_shared_repo_memory_only(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    run_turn: RunTurn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    import daimon.core.repo_memory as memory
    from daimon.core.config import RepoMemorySettings

    await db_session.commit()
    prefix = AsyncMock(return_value="<repo_memory_state>Shared rule</repo_memory_state>\n")
    monkeypatch.setattr(memory, "turn_memory_prefix", prefix)
    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsAgentMessageEvent(
            id="msg",
            type="agent.message",
            processed_at=_NOW,
            content=[BetaManagedAgentsTextBlock(type="text", text="done")],
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id="idle",
            type="session.status_idle",
            processed_at=_NOW,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    ]
    sent: list[dict[str, Any]] = []
    client = _build_client(events, send_capture=sent)
    await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="hi",
        origin="routine",
        session_factory=db_session_factory,
        repo_memory=RepoMemorySettings(enabled=True),
    )
    prefix.assert_awaited_once()
    assert prefix.await_args.kwargs["account_id"] is None, (
        "a routine never adopts its owner's directives"
    )
    assert "Shared rule" in sent[0]["events"][0]["content"][0]["text"]
