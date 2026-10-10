"""Offline real-driver/host codec proofs, with no credentials or live transport."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from anthropic.types.beta.sessions import BetaManagedAgentsEventParams
from daimon.core.mux_backend import TurnBackendRequest
from daimon.core.turn.driver import run_turn
from daimon.core.turn.gemini import PROFILE, GeminiTurnIO
from daimon.core.turn.io import TurnConnectionLost
from daimon.core.turn.posture import BillingExempt
from daimon.testing.ma_transport import ScriptedTransport
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import AgentSpec, EnvironmentSpec, SessionSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.fake import FakeTransport, MemoryStorage
from mux.drivers.gemini.transport import Object, OwnedStream
from mux.errors import ScopeViolation, UnsupportedCapability
from mux.state.memory import MemoryStateStore

SCOPE = Scope(tenant_id="t", account_id="a", principal_id="host", authorization_id="offline")
CHANNEL = ChannelRef(tenant_id="t", platform="test", channel_id="c")
THREAD = ThreadRef(channel=CHANNEL, thread_id="thread")


def interaction(id_: str, *, status: str = "completed", text: str = "answer") -> Object:
    stamp = datetime.now(UTC).isoformat()
    return {
        "id": id_,
        "status": status,
        "created": stamp,
        "updated": stamp,
        "environment_id": "workspace",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": text}]}],
        "usage": {
            "total_input_tokens": 64,
            "total_cached_tokens": 16,
            "total_output_tokens": 8,
            "total_thought_tokens": 3,
        },
    }


class TrackedTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.opened: list[str] = []
        self.close_count = 0
        self.block = False
        self.read_started = asyncio.Event()

    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]:
        self.opened.append(interaction_id)
        if not self.block:
            return await super().open_stream(interaction_id, after=after)

        async def wait() -> AsyncIterator[Object]:
            self.read_started.set()
            await asyncio.Event().wait()
            if False:
                yield {}

        async def close() -> None:
            self.close_count += 1

        return OwnedStream(wait(), close)


@dataclass
class Harness:
    backend: GeminiManagedAgents
    transport: TrackedTransport
    storage: MemoryStorage
    state: MemoryStateStore
    session: ResourceRef


@pytest.fixture
async def harness() -> Harness:
    transport, storage, state = TrackedTransport(), MemoryStorage(), MemoryStateStore()
    backend = GeminiManagedAgents(
        transport, storage=storage, state_store=state, account_scope_id="project"
    )
    agent = await backend.agents.create(
        SCOPE,
        AgentSpec(
            name="offline-host",
            model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
        ),
        key="agent",
    )
    environment = await backend.environments.create(
        SCOPE, EnvironmentSpec(name="inline"), key="env"
    )
    session = await backend.sessions.create(
        SCOPE,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=environment.ref,
            config_revision=1,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session",
                    version=1,
                    value={"binding_id": "binding", "thread": THREAD.model_dump(mode="json")},
                )
            },
        ),
        key="session",
    )
    return Harness(backend, transport, storage, state, session.ref)


@pytest.mark.asyncio
async def test_cold_close_never_opens_a_provider_stream(harness: Harness) -> None:
    io = GeminiTurnIO(harness.backend, SCOPE, harness.session)
    stream = await io.open_stream(read_timeout_s=1)
    assert not harness.transport.opened and not harness.transport.requests
    await stream.close()
    await stream.close()
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
    assert not harness.transport.opened


@pytest.mark.asyncio
async def test_two_host_turns_lazy_stream_reuse_and_real_interaction_usage(
    harness: Harness,
) -> None:
    harness.transport.responses.extend(
        [interaction("first", text="one"), interaction("second", text="two")]
    )
    for id_, text in (("first", "one"), ("second", "two")):
        io = GeminiTurnIO(harness.backend, SCOPE, harness.session)
        stream = await io.open_stream(read_timeout_s=1)
        assert harness.transport.opened == ([] if id_ == "first" else ["first"])
        await io.send([{"type": "user.message", "content": [{"type": "text", "text": "question"}]}])
        frames = [frame async for frame in stream]
        assert [
            frame.native.content[0].text
            for frame in frames
            if frame.native is not None
            and frame.native.type == "agent.message"
            and frame.native.content
        ] == [text]
        observations = [frame.usage for frame in frames if frame.usage is not None]
        assert len(observations) == 1
        assert all(frame.native is None for frame in frames if frame.usage is not None)
        usage = observations[0]
        assert usage.id == f"gemini:{id_}:usage" and usage.turn_id == id_
        assert usage.grain == "turn" and usage.basis == "cumulative"
        assert usage.output_tokens == 11 and usage.output_reasoning_tokens == 3
        assert usage.input_cached_tokens == 16 and usage.input_cache_write_tokens is None
        assert all(
            frame.native is None or frame.native.type != "span.model_request_end"
            for frame in frames
        )
        assert [u.id for u in await io.replay_usage()] == [usage.id]
        assert harness.transport.stream_closed
    assert harness.transport.requests[1]["previous_interaction_id"] == "first"
    assert harness.transport.requests[1]["environment"] == "workspace"
    assert harness.transport.opened == ["first", "second"]


@pytest.mark.asyncio
async def test_restart_replay_retains_usage_and_only_current_root_is_billable(
    harness: Harness,
) -> None:
    original = GeminiTurnIO(harness.backend, SCOPE, harness.session)
    harness.transport.responses.extend([interaction("old"), interaction("current")])
    batch: list[BetaManagedAgentsEventParams] = [
        {"type": "user.message", "content": [{"type": "text", "text": "hi"}]}
    ]
    await original.send(batch)
    await original.send(batch)
    restarted = GeminiManagedAgents(
        harness.transport,
        storage=harness.storage,
        state_store=harness.state,
        account_scope_id="project",
    )
    io = GeminiTurnIO(restarted, SCOPE, harness.session)
    frames = await io.replay_turn_events()
    assert {frame.usage.id for frame in frames if frame.usage} == {
        "gemini:old:usage",
        "gemini:current:usage",
    }
    assert [u.id for u in await io.replay_usage()] == ["gemini:current:usage"]
    changed = interaction("current")
    changed["usage"] = {
        "total_input_tokens": 64,
        "total_cached_tokens": 16,
        "total_output_tokens": 12,
        "total_thought_tokens": 3,
    }
    harness.transport.saved["current"] = changed
    newest = await io.replay_usage()
    assert len(newest) == 1 and newest[0].revision == 2 and newest[0].output_tokens == 15
    assert len(harness.transport.requests) == 2


@pytest.mark.asyncio
async def test_hot_close_cancels_reader_and_closes_owned_source_once(harness: Harness) -> None:
    harness.transport.responses.append(interaction("running", status="in_progress"))
    harness.transport.block = True
    io = GeminiTurnIO(harness.backend, SCOPE, harness.session)
    stream = await io.open_stream(read_timeout_s=5)
    await io.send([{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}])
    for _ in await io.replay_turn_events(reconcile=False):
        await stream.__anext__()  # Drain saved input/usage/running records.
    reader = asyncio.create_task(stream.__anext__())
    await asyncio.wait_for(harness.transport.read_started.wait(), timeout=1)
    await stream.close()
    await stream.close()
    assert reader.cancelled() and harness.transport.close_count == 1


@pytest.mark.asyncio
async def test_eof_without_root_outcome_is_a_replayable_connection_loss(harness: Harness) -> None:
    harness.transport.responses.append(interaction("running", status="in_progress"))
    io = GeminiTurnIO(harness.backend, SCOPE, harness.session)
    stream = await io.open_stream(read_timeout_s=1)
    await io.send([{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}])
    with pytest.raises(TurnConnectionLost):
        _ = [frame async for frame in stream]
    assert harness.transport.stream_closed
    assert not any(
        e.native is not None and e.native.type == "session.status_idle"
        for e in await io.replay_turn_events()
    )


@pytest.mark.asyncio
async def test_restart_cancel_uses_provider_root_and_requires_observed_stop(
    harness: Harness,
) -> None:
    harness.transport.responses.append(interaction("running", status="in_progress"))
    io = GeminiTurnIO(harness.backend, SCOPE, harness.session)
    await io.send([{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}])
    restarted = GeminiTurnIO(harness.backend, SCOPE, harness.session)
    harness.transport.reads["running"] = [
        interaction("running", status="in_progress"),
        interaction("running", status="in_progress"),
        interaction("running", status="cancelled"),
    ]
    stopped = await restarted.interrupt(timeout_s=1)
    assert stopped.stopped and stopped.outcome == "interrupted"
    assert harness.transport.cancelled == ["running"] and len(harness.transport.requests) == 1


def test_foreign_tenant_refuses_before_any_io(harness: Harness) -> None:
    foreign = harness.session.model_copy(update={"tenant_id": "foreign"})
    with pytest.raises(ScopeViolation):
        GeminiTurnIO(harness.backend, SCOPE, foreign)
    assert not harness.transport.requests and not harness.transport.opened


@pytest.mark.asyncio
async def test_n4_dispatch_runs_two_offline_host_turns_without_anthropic_calls(
    harness: Harness,
) -> None:
    revision = ConfigRevision.create(
        CHANNEL,
        1,
        resolve_default(
            BackendConfig(
                backend="gemini",
                profile=PROFILE,
                model="gemini-3.8-flash",
            )
        ),
    )
    default = ConfigRevision.create(CHANNEL, 1, resolve_default(BackendConfig()))
    digest = default.digest
    ancillary = ScriptedTransport()
    harness.transport.responses.extend(
        [
            interaction("host-one", text="first answer"),
            interaction("host-two", text="second answer"),
        ]
    )
    async with ancillary.client() as client:
        for text in ("first answer", "second answer"):
            lifecycle = RecordingLifecycle()
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
                ),
            )
            assert state.content[0].kind == "text" and state.content[0].text == text
            assert len(lifecycle.terminal_success) == 1
    assert not ancillary.requests
    assert ConfigRevision.create(CHANNEL, 1, resolve_default(BackendConfig())).digest == digest
    assert default.backend == "anthropic"
    assert harness.transport.requests[1]["previous_interaction_id"] == "host-one"


@pytest.mark.asyncio
async def test_function_result_uses_native_action_id_and_keeps_root(harness: Harness) -> None:
    reply = interaction("action", status="requires_action")
    reply["steps"] = [
        {
            "type": "function_call",
            "id": "native-call",
            "name": "weather",
            "arguments": {"city": "Oslo"},
        }
    ]
    harness.transport.responses.extend([reply, interaction("resumed")])
    io = GeminiTurnIO(harness.backend, SCOPE, harness.session)
    await io.send([{"type": "user.message", "content": [{"type": "text", "text": "question"}]}])
    frames = await io.replay_turn_events()
    tool = next(
        f.native
        for f in frames
        if f.native is not None and f.native.type == "agent.custom_tool_use"
    )
    root = io.root
    with pytest.raises(UnsupportedCapability, match="unknown_function_action"):
        await io.send(
            [
                {
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": "unknown",
                    "content": [{"type": "text", "text": "cold"}],
                }
            ]
        )
    assert len(harness.transport.requests) == 1
    await io.send(
        [
            {
                "type": "user.custom_tool_result",
                "custom_tool_use_id": tool.id,
                "content": [{"type": "text", "text": "cold"}],
            }
        ]
    )
    assert io.root == root
    assert harness.transport.requests[1]["input"] == [
        {"type": "function_result", "name": "weather", "call_id": "native-call", "result": "cold"}
    ]
    assert {u.id for u in await io.replay_usage()} == {
        "gemini:action:usage",
        "gemini:resumed:usage",
    }


@pytest.mark.asyncio
async def test_expired_workspace_never_opens_a_fresh_host_thread(harness: Harness) -> None:
    from mux.errors import ContinuityLost

    harness.transport.responses.append(interaction("accepted"))
    io = GeminiTurnIO(harness.backend, SCOPE, harness.session)
    batch: list[BetaManagedAgentsEventParams] = [
        {"type": "user.message", "content": [{"type": "text", "text": "hi"}]}
    ]
    await io.send(batch)
    harness.transport.saved.clear()
    with pytest.raises(ContinuityLost):
        await io.send(batch)
    assert len(harness.transport.requests) == 1


@pytest.mark.asyncio
async def test_restart_restores_ack_with_persisted_fallback_model(harness: Harness) -> None:
    from mux.contracts.actions import UserMessage
    from mux.contracts.events import TextPart
    from mux.drivers.gemini.storage import owner

    fallback = ModelRef(provider="gemini", id="gemini-flash-latest")
    first = GeminiManagedAgents(
        harness.transport,
        storage=harness.storage,
        state_store=harness.state,
        account_scope_id="project",
        model_for_interaction=lambda _: fallback,
    )
    harness.transport.responses.append(interaction("fallback"))
    message = (UserMessage(content=(TextPart(text="hello"),)),)
    receipt = await first.events.send(SCOPE, harness.session, message, key="recover-key")
    async with harness.storage.transaction() as records:
        principal, digest, prior = records.sends[(*owner(SCOPE), "recover-key")]
        records.sends[(*owner(SCOPE), "recover-key")] = (
            principal,
            digest,
            prior.model_copy(update={"status": "outcome_unknown"}),
        )
    restarted = GeminiManagedAgents(
        harness.transport,
        storage=harness.storage,
        state_store=harness.state,
        account_scope_id="project",
        model_for_interaction=lambda _: None,
    )
    assert (
        await restarted.events.send(SCOPE, harness.session, message, key="recover-key") == receipt
    )
    observations = await restarted.usage.reconcile(SCOPE, harness.session)
    assert observations and all(u.model == fallback for u in observations)
    assert len(harness.transport.requests) == 1


@pytest.mark.asyncio
async def test_close_disposes_stream_returned_by_cancel_resistant_opener(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mux.contracts.events import Event

    entered = asyncio.Event()
    closed: list[str] = []

    class Source:
        def __aiter__(self) -> AsyncIterator[Event]:
            return self

        async def __anext__(self) -> Event:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            closed.append("closed")

    async def opening(scope: Scope, session: ResourceRef) -> AsyncIterator[Event]:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return Source()
        raise AssertionError("unreachable")

    monkeypatch.setattr(harness.backend.events, "open_stream", opening)
    harness.transport.responses.append(interaction("accepted"))
    io = GeminiTurnIO(harness.backend, SCOPE, harness.session)
    stream = await io.open_stream(read_timeout_s=1)
    await io.send([{"type": "user.message", "content": [{"type": "text", "text": "hello"}]}])
    reader = asyncio.create_task(stream.__anext__())
    await asyncio.wait_for(entered.wait(), timeout=1)
    await stream.close()
    with pytest.raises(StopAsyncIteration):
        await reader
    await stream.close()
    assert closed == ["closed"]
