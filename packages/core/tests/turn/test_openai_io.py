"""Host turn isolation/recovery/cancel through the actual offline OpenAI SDK."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from daimon.core.turn.io import TurnConnectionLost
from daimon.core.turn.openai_io import OpenAITurnIO
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TextBlock, TurnState
from mux.contracts.events import Event
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.receipts import SendReceipt
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from openai import AsyncOpenAI

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="user", authorization_id="host"
)
SESSION = ResourceRef(
    id="session",
    kind="session",
    provider="openai",
    account_scope_id="project",
    tenant_id="tenant",
    account_id="account",
)


class Persistence:
    def __init__(self) -> None:
        self.records: list[Event] = []
        self.operations: list[tuple[str, dict]] = []
        self.refuse_record = False
        self.restored: SendReceipt | None = None
        self.stops: list[str] = []
        self.gaps = 0

    async def stopped(self, key, session):
        assert session == SESSION
        self.stops.append(key)

    async def gap(self, session):
        assert session == SESSION
        self.gaps += 1

    async def record(self, session, event, *, source_key=None):
        assert session == SESSION
        if self.refuse_record:
            raise RuntimeError("offline journal unavailable")
        self.records.append(event)

    async def mutate(self, session, kind, request, deliver, receipt_type, status_of):
        assert session == SESSION
        self.operations.append((kind, request))
        if self.restored is not None:
            return self.restored
        value = await deliver("claimed-" + str(uuid4()))
        assert isinstance(value, receipt_type)
        assert status_of(value) in ("accepted", "processed", "outcome_unknown")
        return value


def turn(identity: str, status="completed", *, child=None):
    return {
        "id": identity,
        "session_id": "session",
        "agent_id": "agent",
        "subagent_id": child,
        "status": status,
        "created_at": 0,
        "usage": {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 10},
            "output_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 5},
        },
    }


def message(identity: str, root: str, text: str, role="assistant"):
    return {
        "id": identity,
        "turn_id": root,
        "type": "message",
        "role": role,
        "status": "completed",
        "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}],
    }


class SSE(httpx.AsyncByteStream):
    def __init__(self, wire: Wire) -> None:
        self.wire = wire
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for value in self.wire.live:
            yield ("data: " + json.dumps(value) + "\n\n").encode()
        if self.wire.eof:
            return
        # Recovery must keep consuming while all snapshot pages are read.
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


class Wire:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.turns = [turn("old")]
        self.items = [message("old-answer", "old", "previous answer")]
        self.live: list[dict] = []
        self.sources: list[SSE] = []
        self.running = False
        self.waits = 0
        self.delay_stop = False
        self.foreign_usage = False
        self.eof = False

    def event(self, suffix, id_, **fields):
        return {
            "type": "agent.session." + suffix,
            "event_id": id_,
            "session_id": "session",
            **fields,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/v1")
        if path.endswith("/events"):
            if request.method == "GET":
                assert request.url.params["stream"] == "true"
                assert request.headers["Accept"] == "text/event-stream"
                source = SSE(self)
                self.sources.append(source)
                return httpx.Response(
                    200, stream=source, headers={"Content-Type": "text/event-stream"}
                )
            assert request.headers["Idempotency-Key"].startswith("claimed-")
            body = json.loads(request.content)
            if body["events"][0]["type"] == "agent.session.input.cancel":
                self.running = False
            else:
                assert body == {
                    "events": [
                        {
                            "type": "agent.session.input.message",
                            "input": [
                                {
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "second question"}],
                                }
                            ],
                        }
                    ]
                }
                self.turns.append(turn("root", "in_progress"))
                self.items.extend(
                    [
                        message("input", "root", "second question", "user"),
                        message("answer", "root", "current answer"),
                    ]
                )
                self.running = True
            return httpx.Response(202, json={})
        if path == "/agents/sessions/session":
            return httpx.Response(
                200,
                json={
                    "id": "session",
                    "agent": {"id": "agent", "model": "gpt-6-luna"},
                    "created_at": 0,
                    "environment": {"type": "openai_hosted", "id": "environment"},
                    "status": "in_progress" if self.running else "idle",
                    "required_actions": [],
                    "metadata": {"mux_tenant": "tenant"},
                },
            )
        if path.endswith("/turns/root"):
            self.waits += 1
            return httpx.Response(
                200,
                json=turn(
                    "root", "in_progress" if self.delay_stop and self.waits == 1 else "cancelled"
                ),
            )
        if path.endswith("/turns"):
            values = self.turns
            if self.foreign_usage:
                values = [{**turn("root"), "session_id": "foreign"}]
        else:
            assert path.endswith("/items")
            values = self.items
        return httpx.Response(
            200,
            json={
                "data": values,
                "has_more": False,
                "last_id": values[-1]["id"] if values else None,
            },
        )


@pytest.fixture
async def host():
    wire, persistence = Wire(), Persistence()
    async with AsyncOpenAI(
        api_key="offline-placeholder",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
    ) as sdk:
        backend = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, kind, id_: scope == SCOPE,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
        )
        yield (
            OpenAITurnIO(backend, SCOPE, SESSION, persistence=persistence),
            backend,
            wire,
            persistence,
        )


async def send(io):
    await io.send(
        [{"type": "user.message", "content": [{"type": "text", "text": "second question"}]}]
    )


@pytest.mark.asyncio
async def test_live_second_turn_keeps_neutral_provenance_and_journals_before_display(host):
    io, _, wire, persistence = host
    wire.live = [
        wire.event("turn.in_progress", "running", turn=turn("root", "in_progress")),
        wire.event(
            "turn.item.done",
            "answer-event",
            turn_id="root",
            item=message("answer", "root", "current answer"),
        ),
        wire.event("turn.completed", "done", turn=turn("root")),
    ]
    source = await io.open_stream(read_timeout_s=2)
    await send(io)
    state = TurnState()
    for _ in range(3):
        frame = await source.__anext__()
        assert frame.normalized in persistence.records and frame.usage is None
        state = apply(state, frame.native)
    await source.close()
    assert wire.sources[0].closed
    assert [part.text for part in state.content if isinstance(part, TextBlock)] == [
        "current answer"
    ]
    assert state.usage_totals == TurnState().usage_totals


@pytest.mark.asyncio
async def test_recovery_renders_current_items_before_terminal_and_excludes_first_turn(host):
    io, _, wire, persistence = host
    await send(io)
    wire.running = False
    wire.turns[-1] = turn("root")
    values = await io.replay(timeout_s=2)
    assert [value.type for value in values] == [
        "user.message",
        "agent.message",
        "session.status_idle",
    ]
    assert values[-1].id == "openai:turn:root:ended"
    assert any(event.turn_id == "old" for event in persistence.records)
    assert all("old" not in value.id for value in values)
    state = TurnState()
    for value in values:
        state = apply(state, value)
    assert [part.text for part in state.content if isinstance(part, TextBlock)] == [
        "current answer"
    ]
    assert wire.sources[-1].closed


@pytest.mark.asyncio
async def test_restart_requires_explicit_root_binding_and_usage_excludes_history_and_children(host):
    _, backend, wire, persistence = host
    wire.turns.extend([turn("root"), turn("child", child="subagent")])
    wire.items.append(message("answer", "root", "current answer"))
    io = OpenAITurnIO(backend, SCOPE, SESSION, persistence=persistence, root_turn_id="root")
    values = await io.replay(timeout_s=2)
    assert [value.id for value in values] == ["openai:item:answer", "openai:turn:root:ended"]
    usage = await io.replay_usage()
    assert len(usage) == 1 and usage[0].turn_id == "root"
    assert usage[0].model is None and usage[0].input_cache_write_tokens is None
    assert usage[0].input_tokens == 100 and usage[0].output_tokens == 20
    unbound = OpenAITurnIO(backend, SCOPE, SESSION, persistence=persistence)
    assert await unbound.replay_usage() == ()


@pytest.mark.asyncio
async def test_unknown_send_and_restored_receipt_never_issue_a_second_post(host):
    io, _, wire, persistence = host
    persistence.restored = SendReceipt(operation_id="saved", status="outcome_unknown", input_ids=())
    with pytest.raises(TurnConnectionLost):
        await send(io)
    with pytest.raises(UnsupportedCapability):
        await send(io)
    assert not any(request.method == "POST" for request in wire.requests)


@pytest.mark.asyncio
async def test_journal_failure_prevents_display_delivery(host):
    io, _, wire, persistence = host
    wire.live = [wire.event("turn.in_progress", "running", turn=turn("root", "in_progress"))]
    source = await io.open_stream(read_timeout_s=2)
    await send(io)
    persistence.refuse_record = True
    with pytest.raises(RuntimeError, match="journal unavailable"):
        await source.__anext__()
    await source.close()


@pytest.mark.asyncio
async def test_cancel_uses_actual_root_and_polls_without_resending_cancel(host):
    io, _, wire, persistence = host
    await send(io)
    wire.delay_stop = True
    stopped = await io.interrupt(timeout_s=2)
    assert stopped.stopped and stopped.outcome == "interrupted"
    assert wire.waits == 2
    assert [kind for kind, _ in persistence.operations] == ["send", "cancel"]
    assert persistence.operations[-1][1] == {"turn_id": "root"}
    cancels = [
        request
        for request in wire.requests
        if request.method == "POST"
        and json.loads(request.content)["events"][0]["type"] == "agent.session.input.cancel"
    ]
    assert len(cancels) == 1
    assert persistence.records[-1].type == "session.turn_ended"
    assert persistence.records[-1].turn_id == "root"
    assert persistence.records[-1].payload["outcome"] == "interrupted"
    assert persistence.stops == [stopped.receipt_operation_id]


@pytest.mark.asyncio
async def test_cancel_without_owned_root_refuses_without_a_write(host):
    io, _, wire, _ = host
    with pytest.raises(UnsupportedCapability):
        await io.interrupt(timeout_s=2)
    assert not any(request.method == "POST" for request in wire.requests)


@pytest.mark.asyncio
async def test_foreign_usage_refuses_instead_of_billing(host):
    _, backend, wire, persistence = host
    io = OpenAITurnIO(backend, SCOPE, SESSION, persistence=persistence, root_turn_id="root")
    wire.foreign_usage = True
    with pytest.raises(ProviderError):
        await io.replay_usage()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        [{"type": "system.message", "content": [{"type": "text", "text": "privileged framing"}]}],
        [{"type": "user.tool_confirmation", "tool_use_id": "anthropic-tool", "result": "allow"}],
    ],
)
async def test_unsupported_privileged_input_and_foreign_confirmation_refuse_before_io(host, events):
    io, _, wire, _ = host
    with pytest.raises(UnsupportedCapability):
        await io.send(events)
    assert wire.requests == []


def test_foreign_account_cannot_construct_host_codec():
    class Backend:
        def capabilities(self):
            from mux.profiles.openai import PERSISTENT_WORKSPACE

            return PERSISTENT_WORKSPACE

    with pytest.raises(ScopeViolation):
        OpenAITurnIO(
            Backend(),
            SCOPE.model_copy(update={"account_id": "other"}),
            SESSION,
            persistence=Persistence(),
        )


@pytest.mark.asyncio
async def test_conflicting_root_outcome_is_refused_before_journal_mutation(host):
    from daimon.core.turn.openai_codec import display_event
    from mux.drivers.openai.normalize import EventNormalizer

    _, backend, _, persistence = host
    io = OpenAITurnIO(backend, SCOPE, SESSION, persistence=persistence, root_turn_id="root")
    completed = EventNormalizer("session").saved_turn(turn("root"))
    cancelled = EventNormalizer("session").saved_turn(turn("root", "cancelled"))
    assert completed is not None and cancelled is not None
    await io.accept_record(completed)
    assert display_event(completed, session_id="session") is not None
    before = list(persistence.records)
    with pytest.raises(ProviderError):
        await io.accept_record(cancelled)
    assert persistence.records == before


@pytest.mark.asyncio
async def test_buffered_current_item_waits_for_explicit_root_and_foreign_child_never_renders(host):
    from mux.drivers.openai.normalize import EventNormalizer

    io, _, _, persistence = host
    await send(io)
    normalizer = EventNormalizer("session")
    item = normalizer.saved_item(message("early", "root", "current answer"))
    child = normalizer.saved_turn(turn("child", "in_progress", child="subagent"))
    root = normalizer.saved_turn(turn("root", "in_progress"))
    assert item is not None and child is not None and root is not None
    assert await io.accept_record(item) == []
    assert await io.accept_record(child) == []
    frames = await io.accept_record(root)
    assert [frame.native.type for frame in frames] == ["agent.message", "session.status_running"]
    assert all(frame.normalized.turn_id == "root" for frame in frames)
    assert child in persistence.records


@pytest.mark.asyncio
async def test_two_new_roots_during_recovery_refuse_ambiguous_attribution_before_journal(host):
    io, _, wire, persistence = host
    await send(io)
    wire.running = False
    wire.turns[-1] = turn("root")
    wire.turns.append(turn("unexpected"))
    with pytest.raises(ProviderError) as caught:
        await io.replay(timeout_s=2)
    assert caught.value.native_code == "ambiguous_host_root"
    assert persistence.records == []


@pytest.mark.asyncio
async def test_eof_without_explicit_root_outcome_checkpoints_gap_and_reconnects(host):
    io, _, wire, persistence = host
    wire.eof = True
    source = await io.open_stream(read_timeout_s=2)
    await send(io)
    with pytest.raises(TurnConnectionLost, match="without a root outcome"):
        await source.__anext__()
    assert persistence.gaps == 1 and wire.sources[-1].closed
    assert not persistence.records
