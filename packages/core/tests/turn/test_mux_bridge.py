"""Exercise the opt-in bridge through real SDK serialization and SSE decoding."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from daimon.core.turn.driver import run_turn
from daimon.core.turn.io import neutral_inputs
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.termination import TerminationReason
from daimon.testing.ma import send_events_response
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.ids import Scope
from mux.drivers.anthropic.actions import translate_inputs
from mux.errors import ScopeViolation

from .conftest import (
    make_agent_message,
    make_end_turn,
    make_requires_action,
    make_retries_exhausted,
    make_status_idle,
    make_status_terminated,
)

_SCOPE = Scope(
    tenant_id="authorized-test-tenant",
    account_id="authorized-test-account",
    principal_id="test-host",
    authorization_id="offline-admitted-turn",
)
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("stop", ["end_turn", "requires_action", "retries_exhausted", "terminated"])
@pytest.mark.parametrize("handoff", [False, True])
@pytest.mark.parametrize("image", [False, True])
async def test_real_http_and_host_effects_match_both_paths(
    stop: str, handoff: bool, image: bool
) -> None:
    message = make_agent_message(event_id="message", text="answer")
    if stop == "terminated":
        ending = make_status_terminated(event_id="ended")
    else:
        reason = {
            "end_turn": make_end_turn(),
            "requires_action": make_requires_action(event_ids=["call"]),
            "retries_exhausted": make_retries_exhausted(),
        }[stop]
        ending = make_status_idle(event_id="ended", stop_reason=reason)
    raw = [message.model_dump(mode="json"), ending.model_dump(mode="json")]
    results = []
    for path in ("legacy", "mux"):
        transport = ScriptedTransport()
        transport.queue(
            ScriptedReply.stream("/v1/sessions/session/events/stream", raw),
            ScriptedReply("POST", "/v1/sessions/session/events", send_events_response()),
        )
        lifecycle = RecordingLifecycle()
        async with transport.client() as client:
            state = await run_turn(
                anthropic=client,
                session_id="session",
                user_message="question",
                lifecycle=lifecycle,
                cancel=asyncio.Event(),
                billing=BillingExempt(reason="headless-unrecorded"),
                path=path,
                scope=_SCOPE,
                now=lambda: _NOW,
                image_blocks=[
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
                    }
                ]
                if image
                else (),
                system_blocks=[{"type": "text", "text": "host framing"}] if handoff else (),
            )
        transport.assert_consumed()
        results.append(
            (
                [request.to_dict() for request in transport.requests],
                state.content,
                state.usage_totals,
                state.stop_reason,
                state.termination,
                None if state.error is None else (state.error.kind, state.error.message),
                [event.model_dump(mode="json") for event in lifecycle.sse_events],
                len(lifecycle.terminal_success),
                len(lifecycle.terminal_failures),
            )
        )
    assert results[0] == results[1]
    assert results[1][0][0]["method"] == "GET"
    assert results[1][0][1]["method"] == "POST"
    if stop == "end_turn":
        assert results[1][4] == TerminationReason.COMPLETED


@pytest.mark.parametrize("path", ["legacy", "mux"])
async def test_idle_stream_opens_before_post_and_needs_no_first_event(path: str) -> None:
    import json

    import httpx
    from anthropic import AsyncAnthropic

    opened = asyncio.Event()
    release_open = asyncio.Event()
    posted = asyncio.Event()
    close = asyncio.Event()
    requests: list[str] = []

    class IdleBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await posted.wait()
            raw = make_status_idle(event_id="ended", stop_reason=make_end_turn()).model_dump(
                mode="json"
            )
            yield f"event: {raw['type']}\ndata: {json.dumps(raw)}\n\n".encode()

        async def aclose(self) -> None:
            close.set()

    async def dispatch(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        if request.method == "GET":
            opened.set()
            await release_open.wait()
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=IdleBody()
            )
        posted.set()
        return send_events_response()

    async with AsyncAnthropic(
        api_key="offline-fixture",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(dispatch)),
    ) as client:
        turn = asyncio.create_task(
            run_turn(
                anthropic=client,
                session_id="session",
                user_message="question",
                lifecycle=RecordingLifecycle(),
                cancel=asyncio.Event(),
                billing=BillingExempt(reason="headless-unrecorded"),
                path=path,
                scope=_SCOPE,
            )
        )
        try:
            await asyncio.wait_for(opened.wait(), timeout=5)
            assert not posted.is_set()
            assert requests == ["GET"]
            release_open.set()
            state = await asyncio.wait_for(turn, timeout=5)
            assert state.termination == TerminationReason.COMPLETED
            assert requests == ["GET", "POST"]
            assert close.is_set()
        finally:
            if not turn.done():
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)


async def test_mux_scope_is_required_before_any_provider_io() -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        with pytest.raises(ScopeViolation):
            await run_turn(
                anthropic=client,
                session_id="session",
                user_message="question",
                lifecycle=RecordingLifecycle(),
                cancel=asyncio.Event(),
                billing=BillingExempt(reason="headless-unrecorded"),
                path="mux",
            )
    assert transport.requests == []


def test_host_inputs_roundtrip_wire_order_and_deny_text() -> None:
    batch = [
        {"type": "user.message", "content": [{"type": "text", "text": "question"}]},
        {"type": "system.message", "content": [{"type": "text", "text": "host framing"}]},
    ]
    assert translate_inputs(neutral_inputs(batch)) == batch
    deny = [
        {
            "type": "user.tool_confirmation",
            "tool_use_id": "call",
            "result": "deny",
            "deny_message": "stopped",
        }
    ]
    assert translate_inputs(neutral_inputs(deny)) == deny


async def test_future_native_root_idle_preserves_legacy_completion_without_reconnect() -> None:
    raw = [
        {
            "id": "future-idle",
            "type": "session.status_idle",
            "stop_reason": {"type": "future_reason"},
            "processed_at": _NOW.isoformat(),
        }
    ]
    results = []
    for path in ("legacy", "mux"):
        transport = ScriptedTransport()
        transport.queue(
            ScriptedReply.stream("/v1/sessions/session/events/stream", raw),
            ScriptedReply("POST", "/v1/sessions/session/events", send_events_response()),
        )
        lifecycle = RecordingLifecycle()
        async with transport.client() as client:
            state = await run_turn(
                anthropic=client,
                session_id="session",
                user_message="question",
                lifecycle=lifecycle,
                cancel=asyncio.Event(),
                billing=BillingExempt(reason="headless-unrecorded"),
                path=path,
                scope=_SCOPE,
                now=lambda: _NOW,
            )
        transport.assert_consumed()
        assert state.termination == TerminationReason.COMPLETED
        assert state.error is None
        assert len(lifecycle.terminal_success) == 1
        assert lifecycle.terminal_failures == []
        assert [(r.method, r.path) for r in transport.requests] == [
            ("GET", "/v1/sessions/session/events/stream"),
            ("POST", "/v1/sessions/session/events"),
        ]
        results.append([request.to_dict() for request in transport.requests])
    assert results[0] == results[1]
