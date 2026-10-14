"""Main confirmation recovery keeps its wire proof and bounds on both paths."""

import asyncio

import httpx
from daimon.core.turn.driver import run_turn
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.termination import TerminationReason
from daimon.testing.ma import list_response, send_events_response, session_response
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization

from .conftest import make_agent_message, make_requires_action, make_status_idle

_SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="admitted"
)
_REFUSAL = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": "waiting on responses to events [pending]",
    },
}


async def test_sdk_composed_mux_recovery_matches_main_legacy_requests_and_effects() -> None:
    pause = make_status_idle(
        event_id="paused", stop_reason=make_requires_action(event_ids=["pending"])
    )
    idle = make_status_idle(event_id="settled")
    answer = make_agent_message(event_id="answer", text="recovered")
    results: list[object] = []
    for path in ("legacy", "mux"):
        transport = ScriptedTransport()
        transport.queue(
            ScriptedReply.stream("/v1/sessions/session/events/stream", []),
            ScriptedReply(
                "POST", "/v1/sessions/session/events", httpx.Response(400, json=_REFUSAL)
            ),
            ScriptedReply(
                "POST",
                "/v1/sessions/session/events",
                send_events_response(),
                request_json={"events": [{"type": "user.interrupt"}]},
                check_json=True,
            ),
            ScriptedReply(
                "GET", "/v1/sessions/session", session_response(session_id="session", status="idle")
            ),
            ScriptedReply(
                "GET",
                "/v1/sessions/session/events",
                list_response([pause.model_dump(mode="json")]),
            ),
            ScriptedReply(
                "GET", "/v1/sessions/session", session_response(session_id="session", status="idle")
            ),
            ScriptedReply(
                "GET",
                "/v1/sessions/session/events",
                list_response([idle.model_dump(mode="json")]),
            ),
            ScriptedReply.stream(
                "/v1/sessions/session/events/stream",
                [answer.model_dump(mode="json"), idle.model_dump(mode="json")],
            ),
            ScriptedReply("POST", "/v1/sessions/session/events", send_events_response()),
        )
        lifecycle = RecordingLifecycle()
        async with transport.client() as client:
            state = await run_turn(
                anthropic=client,
                session_id="session",
                user_message="retry me",
                lifecycle=lifecycle,
                cancel=asyncio.Event(),
                billing=BillingExempt(reason="headless-unrecorded"),
                path=path,
                scope=_SCOPE,
            )
        transport.assert_consumed()
        assert state.error is None and state.termination == TerminationReason.COMPLETED
        assert len(lifecycle.terminal_success) == 1 and lifecycle.terminal_failures == []
        results.append(
            (
                [request.to_dict() for request in transport.requests],
                state.content,
                [event.model_dump(mode="json") for event in lifecycle.sse_events],
            )
        )
    assert results[0] == results[1]


async def test_injected_backend_refusal_never_uses_ancillary_sdk_for_recovery() -> None:
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply.stream("/v1/sessions/session/events/stream", []),
        ScriptedReply("POST", "/v1/sessions/session/events", httpx.Response(400, json=_REFUSAL)),
    )
    lifecycle = RecordingLifecycle()
    async with transport.client() as client:
        backend = AnthropicManagedAgents(
            client,
            account_scope_id="workspace",
            authorization=ResourceAuthorization(_SCOPE, frozenset({("session", "session")})),
        )
        session = ResourceRef(
            id="session",
            kind="session",
            provider="anthropic",
            account_scope_id="workspace",
            tenant_id=_SCOPE.tenant_id,
            account_id=_SCOPE.account_id,
        )
        state = await run_turn(
            anthropic=client,
            session_id="session",
            user_message="retry me",
            lifecycle=lifecycle,
            cancel=asyncio.Event(),
            billing=BillingExempt(reason="headless-unrecorded"),
            path="mux",
            scope=_SCOPE,
            backend=backend,
            session_ref=session,
        )
    transport.assert_consumed()
    assert state.error is not None and state.error.kind == "upstream"
    assert state.termination == TerminationReason.UPSTREAM
    assert len(lifecycle.terminal_failures) == 1 and lifecycle.terminal_success == []
    assert len(transport.requests) == 2
