"""Confirmation recovery keeps its wire proof and bounds on mux turn I/O."""

import asyncio

import httpx
from daimon.core.turn.driver import run_turn
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import extract_final_response
from daimon.core.turn.termination import TerminationReason
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.testing.drivers.anthropic.ma import list_response, send_events_response, session_response
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport

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


async def test_confirmation_recovery_interrupts_settles_and_resends_through_mux() -> None:
    """A send refused while a confirmation is pending interrupts the session, polls
    until its latest idle no longer awaits an action, then resends on a fresh stream."""
    pause = make_status_idle(
        event_id="paused", stop_reason=make_requires_action(event_ids=["pending"])
    )
    idle = make_status_idle(event_id="settled")
    answer = make_agent_message(event_id="answer", text="recovered")
    latest_idle = (
        ("beta", "true"),
        ("limit", "1"),
        ("order", "desc"),
        ("types[]", "session.status_idle"),
    )
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply.stream("/v1/sessions/session/events/stream", []),
        ScriptedReply("POST", "/v1/sessions/session/events", httpx.Response(400, json=_REFUSAL)),
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
            query=latest_idle,
        ),
        ScriptedReply(
            "GET", "/v1/sessions/session", session_response(session_id="session", status="idle")
        ),
        ScriptedReply(
            "GET",
            "/v1/sessions/session/events",
            list_response([idle.model_dump(mode="json")]),
            query=latest_idle,
        ),
        ScriptedReply.stream(
            "/v1/sessions/session/events/stream",
            [answer.model_dump(mode="json"), idle.model_dump(mode="json")],
        ),
        ScriptedReply(
            "POST",
            "/v1/sessions/session/events",
            send_events_response(),
            request_json={
                "events": [
                    {"type": "user.message", "content": [{"type": "text", "text": "retry me"}]}
                ]
            },
            check_json=True,
        ),
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
            scope=_SCOPE,
        )
    transport.assert_consumed()
    assert state.error is None and state.termination == TerminationReason.COMPLETED
    assert extract_final_response(state.content) == "recovered"
    assert len(lifecycle.terminal_success) == 1 and lifecycle.terminal_failures == []


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
            scope=_SCOPE,
            backend=backend,
            session_ref=session,
        )
    transport.assert_consumed()
    assert state.error is not None and state.error.kind == "upstream"
    assert state.termination == TerminationReason.UPSTREAM
    assert len(lifecycle.terminal_failures) == 1 and lifecycle.terminal_success == []
    assert len(transport.requests) == 2
