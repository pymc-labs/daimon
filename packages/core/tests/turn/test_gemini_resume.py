"""A long Gemini interaction survives idle and failed streams and ends with its answer.

Shapes follow the F2-G staging runs: an Antigravity agent turn with thoughts,
code execution, file tools reported as `function_call`, and daimon-mcp results
that Gemini returns as objects rather than text.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.mux_backend import TurnBackendRequest
from daimon.core.turn import gemini as gemini_turn
from daimon.core.turn.driver import run_turn
from daimon.core.turn.gemini import PROFILE, GeminiTurnIO
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import TerminationReason
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.drivers.gemini.transport import Object, OwnedStream
from mux.errors import ProviderError
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from pydantic import JsonValue

from . import test_gemini_codec as codec
from .test_gemini_codec import CHANNEL, SCOPE, Harness

harness = codec.harness

ANSWER = "SKILL-LAST-LINE: done"
MCP_TEXT = '{"agent": "daimon"}'


def steps() -> list[JsonValue]:
    mcp_result = {
        "result": MCP_TEXT,
        "call_tool_result_json": '{"content": []}',
        "structured_content": {"agent": "daimon"},
    }
    return [
        {"type": "thought", "summary": [{"type": "text", "text": "plan"}]},
        {"type": "code_execution_call", "id": "c1", "arguments": {"code": "ls -la"}},
        {
            "type": "code_execution_result",
            "call_id": "c1",
            "exit_code": 0,
            "is_error": False,
            "result": "total 0\n",
        },
        {"type": "function_call", "id": "f1", "name": "view_file", "arguments": {"path": "a"}},
        {
            "type": "function_result",
            "call_id": "f1",
            "name": "view_file",
            "is_error": False,
            "result": "file body",
        },
        {
            "type": "mcp_server_tool_call",
            "id": "m1",
            "name": "call_tool",
            "server_name": "daimon_mcp",
            "arguments": {},
        },
        {
            "type": "mcp_server_tool_result",
            "call_id": "m1",
            "name": "call_tool",
            "server_name": "daimon_mcp",
            "result": mcp_result,
        },
        {
            "type": "mcp_server_tool_call",
            "id": "m2",
            "name": "call_tool",
            "server_name": "daimon_mcp",
            "arguments": {},
        },
        {
            "type": "mcp_server_tool_result",
            "call_id": "m2",
            "name": "call_tool",
            "server_name": "daimon_mcp",
            # Gemini omits `result` on some calls.
            "result": {"call_tool_result_json": '{"content": []}', "structured_content": {}},
        },
        {"type": "model_output", "content": [{"type": "text", "text": ANSWER}]},
    ]


def interaction(status: str) -> Object:
    stamp = datetime.now(UTC).isoformat()
    return {
        "id": "host",
        "status": status,
        "created": stamp,
        "updated": stamp,
        "environment_id": "workspace",
        # Gemini hides saved steps until the interaction leaves in_progress.
        "steps": steps() if status != "in_progress" else [],
        "usage": {
            "total_input_tokens": 640,
            "total_cached_tokens": 600,
            "total_output_tokens": 9,
            "total_thought_tokens": 70,
        },
    }


def stall() -> AsyncIterator[Object]:
    async def silent() -> AsyncIterator[Object]:
        await asyncio.Event().wait()
        if False:
            yield {}

    async def nothing() -> None:
        return None

    return OwnedStream(silent(), nothing)


def failing(error: ProviderError) -> AsyncIterator[Object]:
    async def fail() -> AsyncIterator[Object]:
        await asyncio.sleep(0.01)
        raise error
        yield {}

    async def nothing() -> None:
        return None

    return OwnedStream(fail(), nothing)


def http_error(category: str, status: int, *, retryable: bool) -> ProviderError:
    """What `transport.normalize_error` yields for an unmapped HTTP status."""
    return ProviderError(category, retryable=retryable, native_error_code=f"http_{status}")


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gemini_turn, "STREAM_RETRY_BASE_S", 0.01)


def script(harness: Harness, streams: list[str | ProviderError]) -> list[int]:
    """Serve one scripted behaviour per stream open: 'stall', 'complete' or an error.

    'complete' finishes the interaction natively and serves the real stream.
    Opens past the script stall.
    """
    harness.transport.responses.append(interaction("in_progress"))
    opened: list[int] = []
    serve = harness.transport.open_stream

    async def open_stream(
        interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]:
        opened.append(1)
        behaviour = streams[len(opened) - 1] if len(opened) <= len(streams) else "stall"
        if isinstance(behaviour, ProviderError):
            return failing(behaviour)
        if behaviour == "complete":
            harness.transport.saved["host"] = interaction("completed")
            return await serve(interaction_id, after=after)
        return stall()

    async def cancel(interaction_id: str) -> Object:
        harness.transport.cancelled.append(interaction_id)
        harness.transport.saved[interaction_id] = interaction("cancelled")
        return harness.transport.saved[interaction_id]

    harness.transport.open_stream = open_stream  # type: ignore[method-assign]
    harness.transport.cancel = cancel  # type: ignore[method-assign]
    return opened


async def turn(harness: Harness, *, deadline_s: float = 30) -> tuple[TurnState, RecordingLifecycle]:
    revision = ConfigRevision.create(
        CHANNEL,
        1,
        resolve_default(BackendConfig(backend="gemini", profile=PROFILE, model="gemini-3.8-flash")),
    )
    lifecycle = RecordingLifecycle()
    async with ScriptedTransport().client() as client:
        state = await run_turn(
            anthropic=client,
            session_id=harness.session.id,
            user_message="question",
            lifecycle=lifecycle,
            cancel=asyncio.Event(),
            billing=BillingExempt(reason="headless-unrecorded"),
            path="mux",
            scope=SCOPE,
            backend=harness.backend,
            session_ref=harness.session,
            profile=PROFILE,
            backend_request=TurnBackendRequest(
                profile=PROFILE,
                client=client,
                scope=SCOPE,
                session_id=harness.session.id,
                session=harness.session,
                config=revision,
                read_timeout_s=0.1,
            ),
            stream_read_timeout_s=0.1,
            deadline=datetime.now(UTC) + timedelta(seconds=deadline_s),
        )
    return state, lifecycle


def answered(state: TurnState, lifecycle: RecordingLifecycle) -> None:
    assert state.error is None and state.termination == TerminationReason.COMPLETED
    assert [c.text for c in state.content if c.kind == "text"] == [ANSWER]
    # Every tool row once, however many streams carried the turn.
    assert sum(c.kind == "tool_use" for c in state.content) == 4
    assert len(lifecycle.terminal_success) == 1 and not lifecycle.terminal_failures


@pytest.mark.asyncio
async def test_completed_agent_turn_with_object_mcp_results_delivers_the_answer(
    harness: Harness,
) -> None:
    script(harness, ["complete"])
    answered(*await turn(harness))
    io = GeminiTurnIO(harness.backend, SCOPE, harness.session)
    results = [e for e in await io.replay() if e.type == "agent.tool_result"]
    texts = [json.loads(e.model_dump_json())["content"][0]["text"] for e in results]
    # Object results display as their text; the journal keeps them native.
    assert MCP_TEXT in texts and '{"content": []}' in texts
    assert harness.transport.cancelled == []


@pytest.mark.asyncio
async def test_r11_two_idle_reconnects_then_a_stream_5xx_still_finishes(
    harness: Harness,
) -> None:
    opened = script(
        harness,
        ["stall", "stall", http_error("upstream", 500, retryable=True), "complete"],
    )
    answered(*await turn(harness))
    assert len(opened) >= 4 and len(harness.transport.requests) == 1  # never re-sent
    assert harness.transport.cancelled == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        http_error("upstream", 408, retryable=False),  # unmapped 4xx on a long stream
        http_error("upstream", 502, retryable=True),
        ProviderError("overloaded", retryable=True, native_code="503"),
        ProviderError("transient_network", retryable=True),
    ],
)
async def test_status_only_stream_failures_on_open_or_read_keep_the_turn(
    harness: Harness, error: ProviderError
) -> None:
    script(harness, [error, error, "complete"])
    answered(*await turn(harness))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ProviderError("permission", retryable=False, native_code="gcs_token_echo"),
        ProviderError("upstream", retryable=False, native_code="malformed_interaction"),
        http_error("upstream", 405, retryable=False),  # an unmapped 4xx is not a blip
        ProviderError("upstream", retryable=False),  # status-less SDK error (validation)
    ],
)
async def test_definite_stream_failures_end_the_turn_and_stop_the_interaction(
    harness: Harness, error: ProviderError
) -> None:
    script(harness, [error])
    state, lifecycle = await turn(harness)
    assert state.termination == TerminationReason.UPSTREAM
    assert len(lifecycle.terminal_failures) == 1
    # The host gave up, so Gemini must not keep running (and spending).
    assert harness.transport.cancelled == ["host"]


@pytest.mark.asyncio
async def test_persistent_stream_failure_is_the_ceiling_and_stops_the_interaction(
    harness: Harness,
) -> None:
    script(harness, [http_error("upstream", 500, retryable=True)] * 1000)
    state, lifecycle = await turn(harness, deadline_s=1.0)
    assert state.termination == TerminationReason.CEILING
    assert len(lifecycle.terminal_failures) == 1
    assert harness.transport.cancelled == ["host"]
    assert len(harness.transport.requests) == 1


@pytest.mark.asyncio
async def test_abandon_leaves_a_finished_interaction_alone(harness: Harness) -> None:
    harness.transport.responses.append(interaction("completed"))
    io = GeminiTurnIO(harness.backend, SCOPE, harness.session)
    await io.send([{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}])
    await io.abandon_native()
    assert harness.transport.cancelled == []


@pytest.mark.asyncio
async def test_abandon_is_bounded_and_never_raises(harness: Harness) -> None:
    harness.transport.responses.append(interaction("in_progress"))
    io = GeminiTurnIO(harness.backend, SCOPE, harness.session)
    await io.send([{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}])
    # The cancel is accepted but Gemini never reports the stop.
    await asyncio.wait_for(io.abandon_native(timeout_s=0.2), timeout=2)
    assert harness.transport.cancelled == ["host"]


@pytest.mark.asyncio
async def test_rate_limited_stream_keeps_its_finalization_and_is_not_abandoned(
    harness: Harness,
) -> None:
    script(harness, [http_error("rate_limited", 429, retryable=True)])
    state, lifecycle = await turn(harness)
    assert state.termination == TerminationReason.RATE_LIMITED
    assert len(lifecycle.terminal_failures) == 1
    # A rate-limited provider takes no extra calls; the interaction is left alone.
    assert harness.transport.cancelled == []


@pytest.mark.asyncio
async def test_abandon_cancels_only_this_turns_interaction(harness: Harness) -> None:
    harness.transport.responses.append(interaction("in_progress"))
    io = GeminiTurnIO(harness.backend, SCOPE, harness.session)
    await io.send([{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}])
    io.root = "an-earlier-turn"
    await io.abandon_native()
    assert harness.transport.cancelled == []
