"""Recorded HTTP/SSE fixtures exercise the real SDK without network fallback."""

import asyncio
import json
import pickle
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMCPToolResultEvent,
    BetaManagedAgentsAgentToolResultEvent,
    BetaManagedAgentsUserCustomToolResultEvent,
)
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.actions import NativeInput, UserMessage, UserToolConfirmation, UserToolResult
from mux.contracts.events import Event, TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import PageRequest, ResourceRef, Scope
from mux.contracts.ports import Events
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.normalize import EventNormalizer
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.turn import AnthropicEvents
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability

NOW = datetime(2026, 10, 9, tzinfo=UTC)
SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="caller", authorization_id="grant"
)
SESSION = ResourceRef(
    id="sess_1",
    kind="session",
    provider="anthropic",
    account_scope_id="workspace",
    tenant_id="tenant",
    account_id="account",
)
AUTHORIZATION = ResourceAuthorization(SCOPE, frozenset({("session", "sess_1")}))
ROOT = Path(__file__).resolve().parents[5]


def port(client):
    return AnthropicEvents(client, "workspace", AUTHORIZATION)


def native(kind, id="event", **fields):
    return {"type": kind, "id": id, "processed_at": NOW.isoformat(), **fields}


def fixture_stream(path):
    transcript = json.loads(path.read_text())
    return [
        effect["payload"]["args"][0]
        for effect in transcript["effects"]
        if effect["operation"] == "on_sse_event"
    ]


STREAMS = [
    (path.stem, fixture_stream(path))
    for path in sorted((ROOT / "tests/golden").glob("*.json"))
    if fixture_stream(path)
]


def restore_times(value):
    if isinstance(value, dict):
        return {key: restore_times(item) for key, item in value.items()}
    if isinstance(value, list):
        return [restore_times(item) for item in value]
    if isinstance(value, str) and value.startswith("<time:"):
        return NOW.isoformat().replace("+00:00", "Z")
    return value


@pytest.mark.parametrize("scenario,recorded", STREAMS, ids=[name for name, _ in STREAMS])
async def test_recorded_streams_cross_the_port_as_owned_events(scenario, recorded):
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply.stream("/v1/sessions/sess_1/events/stream", restore_times(recorded))
    )
    async with transport.client() as sdk:
        events = [event async for event in port(sdk).stream(SCOPE, SESSION)]
    assert len(events) == len(recorded)
    assert [event.sequence for event in events] == list(range(len(events)))
    for raw, event in zip(recorded, events, strict=True):
        assert isinstance(event, Event)
        assert event.native.event_id == raw["id"]
        assert event.native.event_type == raw["type"]
        assert event.authority == "record"
        assert Event.model_validate_json(event.model_dump_json()) == event
        assert pickle.loads(pickle.dumps(event)) == event
        assert event.native.record == restore_times(raw)
        if raw["type"] == "agent.message":
            assert event.payload["content"] == raw["content"]
        if raw["type"] == "span.model_request_end":
            assert event.type == "usage.observed"
            assert event.payload == {"observation_id": raw["id"], "revision": 1}
        if raw["type"] == "session.status_idle":
            stop = raw["stop_reason"]
            if stop["type"] == "end_turn":
                assert event.type == "session.turn_ended"
                assert event.payload["outcome"] == "completed"
                assert event.payload["native_reason"] == "end_turn"
                assert event.payload["root_turn_id"]
            elif stop["type"] == "requires_action":
                assert event.type == "session.requires_action"
                assert [action.id for action in event.typed_payload().actions] == stop["event_ids"]
        if raw["type"] == "session.error":
            error = raw["error"]
            if (
                error["type"] in {"mcp_connection_failed_error", "mcp_authentication_failed_error"}
                and error["retry_status"]["type"] == "exhausted"
            ):
                assert event.type == "tool_server.degraded"
        if raw["type"] == "agent.mcp_tool_use":
            assert event.type == "agent.tool_use"
            assert event.payload["executor"] == "mcp"
            assert event.payload["mcp_server"] == raw["mcp_server_name"]
    transport.assert_consumed()
    request = transport.requests[0]
    assert request.query == (("beta", "true"),)
    assert dict(request.protocol_headers)["anthropic-beta"] == "managed-agents-2026-04-01"


async def test_send_serializes_neutral_inputs_with_no_extra_request():
    transport = ScriptedTransport()
    inputs = [
        UserMessage(content=(TextPart(text="hi"),)),
        UserToolConfirmation(action_id="call", decision="deny", deny_message="no"),
        UserToolResult(action_id="custom", content=(TextPart(text="result"),), is_error=True),
    ]
    transport.queue(
        ScriptedReply(
            "POST",
            "/v1/sessions/sess_1/events",
            httpx.Response(200, json={"data": None}),
            request_json={
                "events": [
                    {"type": "user.message", "content": [{"type": "text", "text": "hi"}]},
                    {
                        "type": "user.tool_confirmation",
                        "tool_use_id": "call",
                        "result": "deny",
                        "deny_message": "no",
                    },
                    {
                        "type": "user.custom_tool_result",
                        "custom_tool_use_id": "custom",
                        "content": [{"type": "text", "text": "result"}],
                        "is_error": True,
                    },
                ]
            },
            check_json=True,
        )
    )
    async with transport.client() as sdk:
        receipt = await port(sdk).send(SCOPE, SESSION, inputs, key="operation")
    assert receipt.operation_id == "operation"
    assert receipt.status == "queued"
    transport.assert_consumed()


async def test_accepted_input_ids_are_preserved():
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "POST",
            "/v1/sessions/sess_1/events",
            httpx.Response(
                200,
                json={
                    "data": [
                        native("user.message", "input", content=[{"type": "text", "text": "hi"}])
                    ]
                },
            ),
        )
    )
    async with transport.client() as sdk:
        receipt = await port(sdk).send(
            SCOPE, SESSION, [UserMessage(content=(TextPart(text="hi"),))], key="send"
        )
    assert receipt.status == "processed"
    assert receipt.input_ids == ("input",)
    transport.assert_consumed()


async def test_list_preserves_native_pagination_and_omitted_arguments():
    transport = ScriptedTransport()
    path = "/v1/sessions/sess_1/events"
    transport.queue(
        ScriptedReply(
            "GET",
            path,
            httpx.Response(
                200,
                json={
                    "data": [native("agent.message", content=[{"type": "text", "text": "hi"}])],
                    "has_more": True,
                    "next_page": "cursor",
                },
            ),
            query=(("beta", "true"),),
        ),
        ScriptedReply(
            "GET",
            path,
            httpx.Response(200, json={"data": [], "has_more": False, "next_page": None}),
            query=(("beta", "true"), ("limit", "2"), ("order", "desc"), ("page", "cursor")),
        ),
    )
    async with transport.client() as sdk:
        events = port(sdk)
        first = await events.list(SCOPE, SESSION, page=PageRequest())
        assert first.has_more and first.next_cursor == "cursor"
        last = await events.list(
            SCOPE, SESSION, page=PageRequest(cursor=first.next_cursor, limit=2, order="desc")
        )
        assert not last.has_more and last.next_cursor is None
    transport.assert_consumed()


@pytest.mark.parametrize("operation", ["list", "stream"])
@pytest.mark.parametrize("content", ["omitted", None, []], ids=["omitted", "null", "empty"])
@pytest.mark.parametrize(
    "model,kind,pairing",
    [
        (BetaManagedAgentsAgentToolResultEvent, "agent.tool_result", "tool_use_id"),
        (BetaManagedAgentsAgentMCPToolResultEvent, "agent.mcp_tool_result", "mcp_tool_use_id"),
        (
            BetaManagedAgentsUserCustomToolResultEvent,
            "user.custom_tool_result",
            "custom_tool_use_id",
        ),
    ],
)
async def test_nullable_tool_results_preserve_pairing_and_native_record(
    operation, content, model, kind, pairing
):
    raw = native(kind, "result", **{pairing: "call"}, is_error=True)
    if content != "omitted":
        raw["content"] = content
    expected_native = model.model_validate(raw).model_dump(mode="json")
    transport = ScriptedTransport()
    if operation == "list":
        transport.queue(
            ScriptedReply(
                "GET",
                "/v1/sessions/sess_1/events",
                httpx.Response(200, json={"data": [raw], "next_page": None}),
            )
        )
    else:
        transport.queue(ScriptedReply.stream("/v1/sessions/sess_1/events/stream", [raw]))
    async with transport.client() as sdk:
        events = (
            (await port(sdk).list(SCOPE, SESSION, page=PageRequest())).data
            if operation == "list"
            else [event async for event in port(sdk).stream(SCOPE, SESSION)]
        )
    assert len(events) == 1
    event = events[0]
    assert event.type == "agent.tool_result"
    assert event.item_id == "result" and event.caused_by == ("call",)
    assert event.payload == {"call_id": "call", "content": [], "is_error": True}
    assert event.native.record == expected_native
    assert event.typed_payload().content == ()
    transport.assert_consumed()


@pytest.mark.parametrize("operation", ["list", "stream"])
@pytest.mark.parametrize("content", ["invalid", {}, [None], [{"type": "text"}]])
async def test_malformed_non_null_result_content_raises_owned_error(operation, content):
    raw = native("agent.tool_result", tool_use_id="call", content=content)
    transport = ScriptedTransport()
    if operation == "list":
        transport.queue(
            ScriptedReply(
                "GET",
                "/v1/sessions/sess_1/events",
                httpx.Response(200, json={"data": [raw], "next_page": None}),
            )
        )
    else:
        transport.queue(ScriptedReply.stream("/v1/sessions/sess_1/events/stream", [raw]))
    async with transport.client() as sdk:
        with pytest.raises(ProviderError) as caught:
            if operation == "list":
                await port(sdk).list(SCOPE, SESSION, page=PageRequest())
            else:
                _ = [event async for event in port(sdk).stream(SCOPE, SESSION)]
    assert caught.value.category == "upstream"
    assert caught.value.native_code == "malformed_event" and not caught.value.retryable
    assert isinstance(caught.value.__cause__, (ValueError, KeyError, TypeError))
    transport.assert_consumed()


@pytest.mark.parametrize("with_record,cursor", [(True, ""), (False, "next")])
async def test_list_discards_terminal_cursor_using_sdk_continuation_truth(with_record, cursor):
    records = [native("agent.message", content=[{"type": "text", "text": "hi"}])]
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions/sess_1/events",
            httpx.Response(200, json={"data": records if with_record else [], "next_page": cursor}),
        )
    )
    async with transport.client() as sdk:
        page = await port(sdk).list(SCOPE, SESSION, page=PageRequest())
    assert len(page.data) == int(with_record)
    assert not page.has_more and page.next_cursor is None
    transport.assert_consumed()


async def test_preview_fragments_do_not_replace_the_final_record():
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply.stream(
            "/v1/sessions/sess_1/events/stream",
            [
                {"type": "event_start", "event": {"id": "message", "type": "agent.message"}},
                {
                    "type": "event_delta",
                    "event_id": "message",
                    "delta": {
                        "type": "content_delta",
                        "index": 1,
                        "content": {"type": "text", "text": "hel"},
                    },
                },
                {
                    "type": "event_delta",
                    "event_id": "message",
                    "delta": {"type": "content_delta", "content": {"type": "text", "text": "lo"}},
                },
                native("agent.message", "message", content=[{"type": "text", "text": "hello"}]),
            ],
        )
    )
    async with transport.client() as sdk:
        events = [event async for event in port(sdk).stream(SCOPE, SESSION, previews=True)]
    assert [event.authority for event in events] == ["preview", "preview", "preview", "record"]
    assert [event.payload["text"] for event in events[1:3]] == ["hel", "lo"]
    assert events[1].payload["content_index"] == 1
    assert len({event.id for event in events}) == 4
    assert events[-1].item_id == events[1].item_id == "message"
    transport.assert_consumed()


@pytest.mark.parametrize(
    "kind,action",
    [
        ("agent.tool_use", "tool_confirmation"),
        ("agent.mcp_tool_use", "tool_confirmation"),
        ("agent.custom_tool_use", "function_result"),
    ],
)
def test_required_actions_retain_call_content_and_thread_routing(kind, action):
    normalizer = EventNormalizer(SESSION)
    normalizer.normalize(
        native(
            kind,
            "call",
            name="run",
            input={"command": "pwd"},
            session_thread_id="subagent",
            evaluated_permission="ask",
        ),
        observed_at=NOW,
    )
    event = normalizer.normalize(
        native(
            "session.status_idle",
            stop_reason={"type": "requires_action", "event_ids": ["call", "missing"]},
        ),
        observed_at=NOW,
    )
    actions = event.typed_payload().actions
    assert event.type == "session.requires_action"
    assert actions[0].kind == action and actions[0].call_id == "call"
    assert actions[0].payload["session_thread_id"] == "subagent"
    assert actions[1].kind == "native"


@pytest.mark.parametrize("retry", ["retrying", "exhausted", "terminal"])
def test_mcp_degradation_and_terminal_errors_are_distinct(retry):
    event = EventNormalizer(SESSION).normalize(
        native(
            "session.error",
            error={
                "type": "mcp_connection_failed_error",
                "mcp_server_name": "tools",
                "retry_status": {"type": retry},
                "message": "offline",
            },
        ),
        observed_at=NOW,
    )
    assert event.type == ("session.error" if retry == "terminal" else "tool_server.degraded")
    assert event.payload["retry_status"] == retry


@pytest.mark.parametrize(
    "code,category",
    [
        ("model_rate_limited_error", "rate_limited"),
        ("model_overloaded_error", "overloaded"),
        ("unknown_error", "upstream"),
    ],
)
def test_error_category_and_native_code(code, category):
    event = EventNormalizer(SESSION).normalize(
        native(
            "session.error",
            error={"type": code, "retry_status": {"type": "terminal"}, "message": "failed"},
        ),
        observed_at=NOW,
    )
    assert event.payload["category"] == category and event.payload["native_code"] == code


async def test_cancel_acknowledges_request_without_claiming_a_stop():
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "POST",
            "/v1/sessions/sess_1/events",
            httpx.Response(200, json={"data": None}),
            request_json={"events": [{"type": "user.interrupt"}]},
            check_json=True,
        )
    )
    async with transport.client() as sdk:
        receipt = await port(sdk).cancel(SCOPE, SESSION, turn_id="turn", key="cancel")
    assert receipt.status == "requested" and receipt.turn_id == "turn"
    transport.assert_consumed()
    event = EventNormalizer(SESSION).normalize(native("user.interrupt"), observed_at=NOW)
    assert event.type == "native.user.interrupt"


@pytest.mark.parametrize("operation", ["send", "cancel"])
async def test_lost_post_acknowledgment_is_outcome_unknown(operation):
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply("POST", "/v1/sessions/sess_1/events", httpx.ReadTimeout("lost ack"))
    )
    async with transport.client() as sdk:
        events = port(sdk)
        receipt = await (
            events.send(SCOPE, SESSION, [], key="lost")
            if operation == "send"
            else events.cancel(SCOPE, SESSION, turn_id="turn", key="lost")
        )
    assert receipt.status == "outcome_unknown"
    transport.assert_consumed()
    assert len(transport.requests) == 1


async def test_sdk_status_error_is_normalized_with_original_cause():
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions/sess_1/events",
            httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "busy"}}),
        )
    )
    async with transport.client() as sdk:
        with pytest.raises(ProviderError) as caught:
            await port(sdk).list(SCOPE, SESSION, page=PageRequest())
    assert caught.value.category == "rate_limited"
    assert caught.value.__cause__ is not None
    transport.assert_consumed()


@pytest.mark.parametrize("operation", ["send", "stream", "list", "cancel"])
async def test_foreign_scope_refuses_before_io(operation):
    transport = ScriptedTransport()
    async with transport.client() as sdk:
        with pytest.raises(ScopeViolation):
            events = port(sdk)
            foreign = SCOPE.model_copy(update={"tenant_id": "other"})
            if operation == "send":
                await events.send(foreign, SESSION, [], key="send")
            elif operation == "stream":
                _ = [item async for item in events.stream(foreign, SESSION)]
            elif operation == "list":
                await events.list(foreign, SESSION, page=PageRequest())
            else:
                await events.cancel(foreign, SESSION, turn_id="turn", key="cancel")
    assert not transport.requests


async def test_native_input_and_unsupported_preconditions_fail_before_io():
    transport = ScriptedTransport()
    async with transport.client() as sdk:
        events = port(sdk)
        with pytest.raises(UnsupportedCapability):
            await events.send(
                SCOPE,
                SESSION,
                [
                    NativeInput(
                        extension=ExtensionConfig(
                            namespace="anthropic.platform_export", version=1, value={"events": []}
                        )
                    )
                ],
                key="native",
            )
        with pytest.raises(UnsupportedCapability):
            await events.send(SCOPE, SESSION, [], key="send", expected_turn="turn")
        with pytest.raises(UnsupportedCapability):
            _ = [item async for item in events.stream(SCOPE, SESSION, after="cursor")]
    assert not transport.requests


async def test_factory_registers_events_and_keeps_injection_composable():
    transport = ScriptedTransport()
    async with transport.client() as sdk:
        backend = AnthropicManagedAgents(
            sdk, account_scope_id="workspace", authorization=AUTHORIZATION
        )
        events: Events = backend.events
        assert isinstance(events, AnthropicEvents)
        overridden = AnthropicManagedAgents(sdk, events=events)
        assert overridden.events is events
    assert not transport.requests


def test_history_preserves_separate_root_turns_and_approval_resumption():
    normalizer = EventNormalizer(SESSION)
    first = normalizer.normalize(native("session.status_running", "run1"), observed_at=NOW)
    paused = normalizer.normalize(
        native(
            "session.status_idle",
            "paused",
            stop_reason={"type": "requires_action", "event_ids": ["missing"]},
        ),
        observed_at=NOW,
    )
    resumed = normalizer.normalize(native("session.status_running", "resume"), observed_at=NOW)
    ended = normalizer.normalize(
        native("session.status_idle", "end1", stop_reason={"type": "end_turn"}), observed_at=NOW
    )
    second = normalizer.normalize(native("session.status_running", "run2"), observed_at=NOW)
    assert first.turn_id == paused.turn_id == resumed.turn_id == ended.turn_id == "run1"
    assert second.turn_id == "run2"
    assert ended.payload["root_turn_id"] == "run1"


def test_retries_exhausted_records_an_errored_turn_with_native_reason():
    # FOLLOWUPS: M0's host preserves COMPLETED for an answer with no settled
    # error, even though this neutral stop record correctly says errored.
    normalizer = EventNormalizer(SESSION)
    normalizer.normalize(native("session.status_running", "root"), observed_at=NOW)
    event = normalizer.normalize(
        native("session.status_idle", "ended", stop_reason={"type": "retries_exhausted"}),
        observed_at=NOW,
    )
    assert event.type == "session.turn_ended"
    assert event.payload == {
        "root_turn_id": "root",
        "outcome": "errored",
        "native_reason": "retries_exhausted",
    }


@pytest.mark.parametrize("permission", ["ask", "allow", "deny", None])
def test_tool_permission_preserves_ask_and_defaults_other_values_to_auto(permission):
    event = EventNormalizer(SESSION).normalize(
        native("agent.tool_use", name="run", input={}, evaluated_permission=permission),
        observed_at=NOW,
    )
    assert event.type == "agent.tool_use"
    assert event.payload["permission"] == ("ask" if permission == "ask" else "auto")


def test_terminated_session_resets_root_identity_and_pending_calls():
    normalizer = EventNormalizer(SESSION)
    normalizer.normalize(native("session.status_running", "old-root"), observed_at=NOW)
    normalizer.normalize(
        native("agent.tool_use", "old-call", name="run", input={}), observed_at=NOW
    )
    terminated = normalizer.normalize(native("session.status_terminated"), observed_at=NOW)
    running = normalizer.normalize(native("session.status_running", "new-root"), observed_at=NOW)
    paused = normalizer.normalize(
        native(
            "session.status_idle",
            stop_reason={"type": "requires_action", "event_ids": ["old-call"]},
        ),
        observed_at=NOW,
    )
    assert terminated.type == "session.status_terminated" and terminated.turn_id == "old-root"
    assert running.turn_id == paused.turn_id == "new-root"
    assert paused.type == "session.requires_action"
    assert paused.typed_payload().actions[0].kind == "native"


def test_unknown_idle_reason_does_not_invent_a_terminal_outcome():
    event = EventNormalizer(SESSION).normalize(
        native("session.status_idle", stop_reason={"type": "future_reason"}), observed_at=NOW
    )
    assert event.type == "native.session.status_idle"


def test_subagent_status_retains_provenance_without_ending_root_turn():
    event = EventNormalizer(SESSION, root_turn_id="root").normalize(
        native(
            "session.thread_status_idle",
            session_thread_id="subagent",
            stop_reason={"type": "end_turn"},
        ),
        observed_at=NOW,
    )
    assert event.type == "agent.thread.status_idle"
    assert event.turn_id == "root" and event.thread_id == "subagent"


class IdleBody(httpx.AsyncByteStream):
    def __init__(self):
        self.reads = 0
        self.closed = False
        self.release = asyncio.Event()

    async def __aiter__(self):
        self.reads += 1
        await self.release.wait()
        yield b""

    async def aclose(self):
        self.closed = True


async def test_open_stream_waits_for_connection_before_send_without_waiting_for_an_event():
    opening = asyncio.Event()
    connected = asyncio.Event()
    requests = []
    body = IdleBody()

    async def handler(request):
        requests.append(request.method)
        if request.method == "GET":
            opening.set()
            await connected.wait()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
        return httpx.Response(200, json={"data": None})

    async with AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as sdk:
        events = port(sdk)

        async def begin():
            stream = await events.open_stream(SCOPE, SESSION)
            try:
                await events.send(
                    SCOPE, SESSION, [UserMessage(content=(TextPart(text="hi"),))], key="send"
                )
            finally:
                await stream.aclose()

        task = asyncio.create_task(begin())
        try:
            await asyncio.wait_for(opening.wait(), timeout=5)
            assert requests == ["GET"]
            assert not task.done()
            connected.set()
            await asyncio.wait_for(task, timeout=5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert requests == ["GET", "POST"]
    assert body.reads == 0
    assert body.closed


async def test_open_stream_can_close_before_the_first_read():
    body = IdleBody()

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    async with AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as sdk:
        stream = await port(sdk).open_stream(SCOPE, SESSION)
        await stream.aclose()
        await stream.aclose()
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
    assert body.reads == 0 and body.closed


async def test_stream_read_error_crosses_the_port_as_a_provider_error():
    class BrokenBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            raise httpx.ReadError("stream lost")
            yield b""  # pragma: no cover

        async def aclose(self):
            self.closed = True

    body = BrokenBody()
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions/sess_1/events/stream",
            httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body),
        )
    )
    async with transport.client() as sdk:
        stream = await port(sdk).open_stream(SCOPE, SESSION)
        with pytest.raises(ProviderError) as caught:
            await stream.__anext__()
    assert caught.value.category == "transient_network" and caught.value.retryable
    assert isinstance(caught.value.__cause__, httpx.ReadError)
    assert body.closed
    transport.assert_consumed()


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "openai"},
        {"account_scope_id": "other"},
        {"account_id": "other"},
        {"kind": "agent"},
    ],
)
async def test_foreign_session_reference_is_rejected_before_io(change):
    transport = ScriptedTransport()
    async with transport.client() as sdk:
        with pytest.raises(ScopeViolation):
            await port(sdk).send(SCOPE, SESSION.model_copy(update=change), [], key="foreign")
    assert not transport.requests


def test_inline_images_are_owned_content_and_native_file_sources_stay_scoped_native():
    raw = native(
        "user.message",
        content=[
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
            },
            {"type": "image", "source": {"type": "file", "file_id": "provider-file"}},
        ],
    )
    event = EventNormalizer(SESSION).normalize(raw, observed_at=NOW)
    assert event.payload["content"][0] == {
        "type": "image",
        "media_type": "image/png",
        "data_base64": "aGVsbG8=",
    }
    assert event.payload["content"][1]["type"] == "native"
    assert event.payload["content"][1]["payload"] == raw["content"][1]
    assert event.native.record == raw
