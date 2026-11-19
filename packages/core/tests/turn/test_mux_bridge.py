"""Exercise the opt-in bridge through real SDK serialization and SSE decoding."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from anthropic.types.beta.sessions import BetaManagedAgentsEventParams
from daimon.core.turn.driver import run_turn
from daimon.core.turn.io import neutral_inputs
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TextBlock
from daimon.core.turn.termination import TerminationReason
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.ids import Scope
from mux.drivers.anthropic.actions import translate_inputs
from mux.errors import ScopeViolation
from mux.testing.drivers.anthropic.ma import send_events_response
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport

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
async def test_real_http_traffic_and_host_effects(stop: str, handoff: bool, image: bool) -> None:
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
    assert [request.method for request in transport.requests] == ["GET", "POST"]
    sent = cast(dict[str, Any], transport.requests[1].json())["events"]
    assert [event["type"] for event in sent] == ["user.message"] + (
        ["system.message"] if handoff else []
    )
    assert [block["type"] for block in sent[0]["content"]] == ["image"] * image + ["text"]
    assert state.content == [TextBlock(kind="text", text="answer")]
    if stop == "end_turn":
        assert state.termination == TerminationReason.COMPLETED
        assert len(lifecycle.terminal_success) == 1


async def test_idle_stream_opens_before_post_and_needs_no_first_event() -> None:
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
            )
    assert transport.requests == []


def test_host_inputs_roundtrip_wire_order_and_deny_text() -> None:
    batch: list[BetaManagedAgentsEventParams] = [
        {"type": "user.message", "content": [{"type": "text", "text": "question"}]},
        {"type": "system.message", "content": [{"type": "text", "text": "host framing"}]},
    ]
    assert translate_inputs(neutral_inputs(batch)) == batch
    deny: list[BetaManagedAgentsEventParams] = [
        {
            "type": "user.tool_confirmation",
            "tool_use_id": "call",
            "result": "deny",
            "deny_message": "stopped",
        }
    ]
    assert translate_inputs(neutral_inputs(deny)) == deny


async def test_future_native_root_idle_completes_without_reconnect() -> None:
    raw: list[dict[str, object]] = [
        {
            "id": "future-idle",
            "type": "session.status_idle",
            "stop_reason": {"type": "future_reason"},
            "processed_at": _NOW.isoformat(),
        }
    ]
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
