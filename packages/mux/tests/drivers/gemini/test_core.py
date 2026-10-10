"""Offline Gemini port probes using documented wire records and no provider access."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone

import pytest
from mux.contracts.actions import NativeInput, UserMessage, UserToolResult
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.events import NativePart, TextPart, ToolResultPayload
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, PageRequest, Scope, ThreadRef
from mux.contracts.ports import ManagedAgents, Steering
from mux.contracts.resources import (
    AgentSpec,
    EnvironmentSpec,
    MCPConnection,
    Session,
    SessionSpec,
    ToolSpec,
)
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.core import compile_agent
from mux.drivers.gemini.fake import FakeTransport, MemoryStorage
from mux.drivers.gemini.transport import Object, close_iterator
from mux.errors import (
    ContinuityLost,
    ExtensionVersionError,
    OperationConflict,
    ProviderError,
    ScopeViolation,
    UnsupportedCapability,
)
from mux.state.memory import MemoryStateStore
from pydantic import JsonValue

SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="allowed")
THREAD = ThreadRef(
    channel=ChannelRef(tenant_id="t", platform="discord", channel_id="c"), thread_id="th"
)
MESSAGE = UserMessage(content=(TextPart(text="hello"),))


def native(id_: str = "i1", status: str = "completed", **extra: JsonValue) -> Object:
    stamp = datetime.now(UTC).isoformat()
    return {
        "id": id_,
        "status": status,
        "created": stamp,
        "updated": stamp,
        "environment_id": "e1",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": "done"}]}],
        "usage": {
            "total_input_tokens": 10,
            "total_cached_tokens": 2,
            "total_output_tokens": 20,
            "total_thought_tokens": 3,
        },
        **extra,
    }


type Setup = tuple[
    GeminiManagedAgents, FakeTransport, MemoryStorage, MemoryStateStore, Session, SessionSpec
]


@pytest.fixture
async def setup() -> Setup:
    t, storage, state = FakeTransport(), MemoryStorage(), MemoryStateStore()
    ma = GeminiManagedAgents(t, storage=storage, state_store=state, account_scope_id="project")
    typed: ManagedAgents = ma
    assert typed is ma
    agent = await ma.agents.create(
        SCOPE,
        AgentSpec(name="a", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        key="agent",
    )
    env = await ma.environments.create(SCOPE, EnvironmentSpec(name="e"), key="env")
    spec = SessionSpec(
        agent=agent.ref,
        agent_revision=agent.revision,
        environment=env.ref,
        config_revision=1,
        extensions={
            "gemini.session": ExtensionConfig(
                namespace="gemini.session",
                version=1,
                value={"thread": THREAD.model_dump(mode="json"), "binding_id": "b"},
            )
        },
    )
    s = await ma.sessions.create(SCOPE, spec, key="session")
    return ma, t, storage, state, s, spec


@pytest.mark.asyncio
async def test_two_turns_reuse_history_environment_and_immutable_default(setup: Setup) -> None:
    ma, t, _, _, s, _ = setup
    assert not t.requests
    t.responses.extend([native(), native("i2")])
    first = await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="one")
    second = await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="two")
    assert first.turn_id != second.turn_id
    assert [request["tools"] for request in t.requests] == [[], []]
    assert t.requests[1]["previous_interaction_id"] == "i1"
    assert t.requests[1]["environment"] == "e1"
    assert t.requests[1]["agent_config"] == {"type": "antigravity", "model": "gemini-3.8-flash"}
    page = await ma.events.list(SCOPE, s.ref, page=PageRequest())
    assert len([e for e in page.data if e.type == "user.message"]) == 2
    assert len([e for e in page.data if e.type == "session.turn_ended"]) == 2
    assert resolve_default(BackendConfig()).backend == "anthropic"
    config = ConfigRevision.create(
        THREAD.channel,
        1,
        resolve_default(
            BackendConfig(backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash")
        ),
    )
    assert "usage_observations" in ma.admit(config).satisfied
    assert not ma.capabilities().core


@pytest.mark.asyncio
async def test_send_is_idempotent_after_restart_and_rejects_conflicting_content(
    setup: Setup,
) -> None:
    ma, t, storage, state, s, _ = setup
    t.responses.append(native())
    receipt = await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="send")
    restarted = GeminiManagedAgents(
        t, storage=storage, state_store=state, account_scope_id="project"
    )
    assert await restarted.events.send(SCOPE, s.ref, (MESSAGE,), key="send") == receipt
    with pytest.raises(OperationConflict):
        await restarted.events.send(
            SCOPE, s.ref, (UserMessage(content=(TextPart(text="different"),)),), key="send"
        )
    assert len(t.requests) == 1


@pytest.mark.asyncio
async def test_uncertain_delivery_never_resends_or_starts_another_root(setup: Setup) -> None:
    ma, t, storage, state, s, _ = setup
    t.responses.append(ProviderError("transient_network", retryable=True))
    receipt = await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="send")
    assert receipt.status == "outcome_unknown"
    restarted = GeminiManagedAgents(
        t, storage=storage, state_store=state, account_scope_id="project"
    )
    assert await restarted.events.send(SCOPE, s.ref, (MESSAGE,), key="send") == receipt
    with pytest.raises(UnsupportedCapability, match="unreconciled_delivery"):
        await restarted.events.send(SCOPE, s.ref, (MESSAGE,), key="new-key")
    assert len(t.requests) == 1


@pytest.mark.asyncio
async def test_cross_tenant_account_provider_and_principal_rejections_precede_io(
    setup: Setup,
) -> None:
    ma, t, _, _, s, _ = setup
    for change in ({"tenant_id": "foreign"}, {"account_id": "foreign"}):
        with pytest.raises(ScopeViolation):
            await ma.sessions.retrieve(SCOPE.model_copy(update=change), s.ref)
    with pytest.raises(ScopeViolation):
        await ma.events.send(
            SCOPE, s.ref.model_copy(update={"account_scope_id": "foreign"}), (MESSAGE,), key="no"
        )
    t.responses.append(native())
    await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="send")
    with pytest.raises(ScopeViolation):
        await ma.events.send(
            SCOPE.model_copy(update={"principal_id": "foreign"}), s.ref, (MESSAGE,), key="send"
        )
    assert len(t.requests) == 1


@pytest.mark.asyncio
async def test_expiry_is_visible_and_never_creates_a_fresh_environment(setup: Setup) -> None:
    ma, t, storage, _, s, _ = setup
    t.responses.append(native())
    await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    async with storage.transaction() as records:
        records.sessions[s.ref.id].workspace_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(ContinuityLost, match="expired") as exc:
        await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="second")
    assert exc.value.binding_id == "b"
    assert len(t.requests) == 1


@pytest.mark.asyncio
async def test_deleted_history_and_replaced_environment_refuse_continuation(setup: Setup) -> None:
    ma, t, _, _, s, _ = setup
    t.responses.append(native())
    await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    t.saved["i1"]["environment_id"] = "replacement"
    with pytest.raises(ContinuityLost, match="replaced"):
        await ma.sessions.retrieve(SCOPE, s.ref)
    t.saved.clear()
    with pytest.raises(ContinuityLost):
        await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="second")
    assert len(t.requests) == 1


@pytest.mark.asyncio
async def test_required_action_continuation_keeps_root_until_real_completion(setup: Setup) -> None:
    ma, t, _, _, s, _ = setup
    t.responses.extend(
        [
            native(
                status="requires_action",
                steps=[
                    {
                        "type": "function_call",
                        "id": "call",
                        "name": "weather",
                        "arguments": {"city": "Oslo"},
                    }
                ],
            ),
            native("i2"),
        ]
    )
    first = await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    waiting = await ma.sessions.retrieve(SCOPE, s.ref)
    assert waiting.state == "requires_action"
    assert waiting.required_actions[0].call_id == "call"
    second = await ma.events.send(
        SCOPE,
        s.ref,
        (UserToolResult(action_id="call", content=(TextPart(text="cold"),)),),
        key="result",
    )
    assert second.turn_id == first.turn_id
    assert [request["tools"] for request in t.requests] == [[], []]
    assert t.requests[1]["input"] == [
        {"type": "function_result", "name": "weather", "call_id": "call", "result": "cold"}
    ]
    await ma.events.reconcile(SCOPE, s.ref)
    page = await ma.events.list(SCOPE, s.ref, page=PageRequest())
    assert len([e for e in page.data if e.type == "session.turn_ended"]) == 1


@pytest.mark.asyncio
async def test_auto_executed_filesystem_call_is_not_a_pending_host_action(setup: Setup) -> None:
    ma, t, _, _, s, _ = setup
    t.responses.extend(
        [
            native(
                status="requires_action",
                steps=[
                    {
                        "type": "function_call",
                        "id": "fs",
                        "name": "write_file",
                        "arguments": {"path": "/workspace/weather.txt", "content": "forecast"},
                    },
                    {
                        "type": "function_result",
                        "call_id": "fs",
                        "name": "write_file",
                        "result": "written",
                    },
                    {
                        "type": "function_call",
                        "id": "custom",
                        "name": "weather",
                        "arguments": {"city": "Oslo"},
                    },
                ],
            ),
            native("i2"),
        ]
    )
    first = await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    waiting = await ma.sessions.retrieve(SCOPE, s.ref)
    assert [a.call_id for a in waiting.required_actions] == ["custom"]
    journal = await ma.events.list(SCOPE, s.ref, page=PageRequest())
    pending = [e for e in journal.data if e.type == "session.requires_action"]
    assert len(pending) == 1
    assert pending[0].payload["actions"] == [waiting.required_actions[0].model_dump(mode="json")]
    with pytest.raises(UnsupportedCapability):
        await ma.events.send(
            SCOPE,
            s.ref,
            (UserToolResult(action_id="fs", content=(TextPart(text="again"),)),),
            key="completed-result",
        )
    assert len(t.requests) == 1
    assert await ma.events.list(SCOPE, s.ref, page=PageRequest()) == journal
    result = await ma.events.send(
        SCOPE,
        s.ref,
        (UserToolResult(action_id="custom", content=(TextPart(text="cold"),)),),
        key="pending-result",
    )
    assert result.turn_id == first.turn_id
    assert t.requests[1]["input"] == [
        {"type": "function_result", "name": "weather", "call_id": "custom", "result": "cold"}
    ]


@pytest.mark.asyncio
async def test_unresolved_native_action_stays_visible_after_completed_calls(setup: Setup) -> None:
    ma, t, _, _, s, _ = setup
    t.responses.append(
        native(
            status="requires_action",
            steps=[
                {"type": "function_call", "id": "fs", "name": "write_file", "arguments": {}},
                {"type": "function_result", "call_id": "fs", "result": "written"},
                {"type": "pending_native_action", "id": "unknown"},
            ],
        )
    )
    await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    waiting = await ma.sessions.retrieve(SCOPE, s.ref)
    assert waiting.state == "requires_action"
    assert len(waiting.required_actions) == 1
    action = waiting.required_actions[0]
    assert action.kind == "native" and action.call_id is None
    with pytest.raises(UnsupportedCapability):
        await ma.events.send(
            SCOPE,
            s.ref,
            (UserToolResult(action_id=action.id, content=(TextPart(text="x"),)),),
            key="unsupported-result",
        )
    journal = await ma.events.list(SCOPE, s.ref, page=PageRequest())
    assert any(e.type == "session.requires_action" for e in journal.data)
    assert not any(e.type == "session.turn_ended" for e in journal.data)
    assert len(t.requests) == 1


@pytest.mark.asyncio
async def test_previews_and_eof_do_not_complete_or_bill_a_turn(setup: Setup) -> None:
    ma, t, _, _, s, _ = setup
    t.responses.append(native(status="in_progress", steps=[], usage=None))
    await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    t.streams["i1"] = [
        {"event_type": "step.delta", "index": 0, "delta": {"type": "text", "text": "preview"}},
        {"event_type": "native.child.completed"},
    ]
    streamed = [e async for e in ma.events.stream(SCOPE, s.ref, previews=True)]
    assert len(streamed) == 1 and streamed[0].authority == "preview"
    assert t.stream_closed
    current = await ma.sessions.retrieve(SCOPE, s.ref)
    assert current.state == "running" and current.active_root_turn is not None
    assert (await ma.usage.reconcile(SCOPE, s.ref))[0].output_tokens is None


@pytest.mark.asyncio
async def test_cancel_receipt_retains_occupancy_until_observed_stop(setup: Setup) -> None:
    ma, t, _, _, s, _ = setup
    t.responses.append(native(status="in_progress", steps=[]))
    sent = await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    assert sent.turn_id is not None
    receipt = await ma.events.cancel(SCOPE, s.ref, turn_id=sent.turn_id, key="cancel")
    assert receipt.status == "requested"
    deadline = datetime.now(UTC) + timedelta(seconds=1)
    assert not (await ma.events.wait_stopped(SCOPE, receipt, deadline=deadline)).stopped
    assert (await ma.sessions.retrieve(SCOPE, s.ref)).state == "running"
    t.saved["i1"]["status"] = "cancelled"
    stop = await ma.events.wait_stopped(SCOPE, receipt, deadline=deadline)
    assert stop.stopped and stop.outcome == "interrupted"
    assert (await ma.sessions.retrieve(SCOPE, s.ref)).state == "idle"


@pytest.mark.asyncio
async def test_usage_null_corrections_and_stale_replay_preserve_identity(setup: Setup) -> None:
    ma, t, _, _, s, _ = setup
    t.responses.append(native(usage=None))
    await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    for count in (100, 120, 110):
        t.saved["i1"]["usage"] = {
            "total_input_tokens": 10,
            "total_output_tokens": count,
            "total_thought_tokens": 0,
        }
        t.saved["i1"]["updated"] = datetime.now(UTC).isoformat()
        await ma.usage.reconcile(SCOPE, s.ref)
    observations = await ma.usage.list(SCOPE, s.ref, page=PageRequest())
    assert [(o.revision, o.output_tokens) for o in observations.data] == [
        (1, None),
        (2, 100),
        (3, 120),
        (4, 110),
    ]
    assert len({o.id for o in observations.data}) == 1
    assert observations.data[-1].input_cached_tokens is None
    await ma.usage.reconcile(SCOPE, s.ref)
    assert await ma.usage.list(SCOPE, s.ref, page=PageRequest()) == observations
    t.saved["i1"]["updated"] = "2026-01-01T00:00:00Z"
    t.saved["i1"]["usage"] = {"total_output_tokens": 999, "total_thought_tokens": 0}
    await ma.usage.reconcile(SCOPE, s.ref)
    assert await ma.usage.list(SCOPE, s.ref, page=PageRequest()) == observations


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("offsets", [False, True])
async def test_equal_meter_advances_native_watermark_without_charge(
    setup: Setup, restart: bool, offsets: bool
) -> None:
    ma, t, storage, state, s, _ = setup
    t0 = datetime.now(UTC) - timedelta(seconds=3)

    def snapshot(seconds: int, count: int, offset: int = 0) -> Object:
        stamp = (t0 + timedelta(seconds=seconds)).astimezone(timezone(timedelta(hours=offset)))
        return native(
            updated=stamp.isoformat(),
            usage={"total_output_tokens": count, "total_thought_tokens": 0},
        )

    t.responses.append(snapshot(0, 10))
    await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    before = await ma.usage.list(SCOPE, s.ref, page=PageRequest())
    t.saved["i1"] = snapshot(2, 10, -6 if offsets else 0)
    assert await ma.usage.reconcile(SCOPE, s.ref) == before.data
    if restart:
        ma = GeminiManagedAgents(
            t, storage=storage, state_store=state.restart(), account_scope_id="project"
        )
    t.saved["i1"] = snapshot(1, 1, 9 if offsets else 0)
    t.saved["i1"]["status"] = "in_progress"
    assert await ma.usage.reconcile(SCOPE, s.ref) == before.data
    assert (await ma.sessions.retrieve(SCOPE, s.ref)).state == "idle"
    journal = await ma.events.list(SCOPE, s.ref, page=PageRequest())
    assert sum(e.type == "usage.observed" for e in journal.data) == 1
    t.saved["i1"] = snapshot(3, 11)
    updated = await ma.usage.reconcile(SCOPE, s.ref)
    assert [(o.revision, o.output_tokens) for o in updated] == [(1, 10), (2, 11)]


@pytest.mark.asyncio
async def test_same_second_fractional_provider_time_is_a_newer_correction(setup: Setup) -> None:
    ma, t, _, _, s, _ = setup
    stamp = datetime.now(UTC).replace(microsecond=0)
    t.responses.append(
        native(
            updated=stamp.isoformat().replace("+00:00", "Z"),
            usage={"total_output_tokens": 10, "total_thought_tokens": 0},
        )
    )
    await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    t.saved["i1"]["updated"] = (
        (stamp + timedelta(microseconds=500000)).isoformat().replace("+00:00", "Z")
    )
    t.saved["i1"]["usage"] = {"total_output_tokens": 11, "total_thought_tokens": 0}
    observations = await ma.usage.reconcile(SCOPE, s.ref)
    assert [(o.revision, o.output_tokens) for o in observations] == [(1, 10), (2, 11)]


@pytest.mark.asyncio
async def test_open_saved_model_output_cannot_truncate_authoritative_history(setup: Setup) -> None:
    ma, t, _, _, s, _ = setup
    response = native(
        status="in_progress",
        steps=[{"type": "model_output", "content": [{"type": "text", "text": "hel"}]}],
    )
    t.responses.append(response)
    await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    await ma.events.reconcile(SCOPE, s.ref)
    partial = await ma.events.list(SCOPE, s.ref, page=PageRequest())
    assert not any(e.type == "agent.message" for e in partial.data)
    assert (await ma.sessions.retrieve(SCOPE, s.ref)).state == "running"
    t.saved["i1"] = native(
        steps=[{"type": "model_output", "content": [{"type": "text", "text": "hello"}]}],
    )
    await ma.events.reconcile(SCOPE, s.ref)
    await ma.events.reconcile(SCOPE, s.ref)
    final = await ma.events.list(SCOPE, s.ref, page=PageRequest())
    messages = [e for e in final.data if e.type == "agent.message"]
    assert len(messages) == 1 and messages[0].authority == "record"
    assert messages[0].payload["content"] == [{"type": "text", "text": "hello"}]
    assert sum(e.type == "session.turn_ended" for e in final.data) == 1


@pytest.mark.asyncio
async def test_usage_includes_thoughts_and_keeps_raw_meter(setup: Setup) -> None:
    ma, t, _, _, s, _ = setup
    response = native()
    t.responses.append(response)
    await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    observation = (await ma.usage.reconcile(SCOPE, s.ref))[0]
    assert observation.output_tokens == 23 and observation.output_reasoning_tokens == 3
    assert observation.input_tokens == 10 and observation.input_cached_tokens == 2
    assert observation.input_cache_write_tokens is None
    assert observation.native_meter == response["usage"]


@pytest.mark.asyncio
async def test_extension_bypass_and_missing_model_are_refused(setup: Setup) -> None:
    ma, t, _, _, s, _ = setup
    with pytest.raises(UnsupportedCapability):
        await ma.events.send(
            SCOPE,
            s.ref,
            (
                NativeInput(
                    extension=ExtensionConfig(namespace="gemini.bypass", version=1, value={})
                ),
            ),
            key="bypass",
        )
    with pytest.raises(ExtensionVersionError):
        ma.extension(Steering, namespace="gemini.session", version=99)
    assert not t.requests


@pytest.mark.asyncio
async def test_open_stream_closes_connection_without_first_iteration(setup: Setup) -> None:
    ma, t, _, _, s, _ = setup
    t.responses.append(native(status="in_progress"))
    await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    opened = await ma.events.open_stream(SCOPE, s.ref)
    await close_iterator(opened)
    assert t.stream_closed


@pytest.mark.asyncio
async def test_cancel_key_replays_without_another_provider_request(setup: Setup) -> None:
    ma, t, _, _, s, _ = setup
    t.responses.append(native(status="in_progress"))
    sent = await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    assert sent.turn_id is not None
    a = await ma.events.cancel(SCOPE, s.ref, turn_id=sent.turn_id, key="cancel")
    b = await ma.events.cancel(SCOPE, s.ref, turn_id=sent.turn_id, key="cancel")
    assert a == b and t.cancelled == ["i1"]


def test_gemini_requires_an_explicit_profile_and_model() -> None:
    from mux.errors import InvalidConfig

    for configuration in (
        BackendConfig(backend="gemini", model="gemini-3.8-flash"),
        BackendConfig(backend="gemini", profile="gemini.inline_reuse"),
    ):
        with pytest.raises(InvalidConfig):
            resolve_default(configuration)


@pytest.mark.asyncio
async def test_crash_after_provider_acceptance_retains_uncertainty_across_restart(
    setup: Setup,
) -> None:
    _, _, storage, state, s, _ = setup

    class CrashTransport(FakeTransport):
        async def create(self, request: Mapping[str, JsonValue]) -> Object:
            await super().create(request)
            raise RuntimeError("synthetic process death after provider acceptance")

    crash = CrashTransport()
    crash.responses.append(native())
    first = GeminiManagedAgents(
        crash, storage=storage, state_store=state, account_scope_id="project"
    )
    with pytest.raises(RuntimeError, match="synthetic process death"):
        await first.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    assert "i1" in crash.saved  # upstream accepted independently of local rollback
    restarted = GeminiManagedAgents(
        crash, storage=storage, state_store=state, account_scope_id="project"
    )
    receipt = await restarted.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    assert receipt.status == "outcome_unknown"
    with pytest.raises(UnsupportedCapability, match="unreconciled_delivery"):
        await restarted.events.send(SCOPE, s.ref, (MESSAGE,), key="new-key")
    assert len(crash.requests) == 1


@pytest.mark.asyncio
async def test_malformed_post_acknowledgement_cannot_release_send_guard(setup: Setup) -> None:
    ma, t, _, _, s, _ = setup
    t.responses.append({"id": "i1"})
    with pytest.raises(ProviderError, match="malformed_interaction"):
        await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    with pytest.raises(UnsupportedCapability, match="unreconciled_delivery"):
        await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="new-key")
    assert len(t.requests) == 1


@pytest.mark.asyncio
async def test_saved_host_builtin_and_mcp_tools_keep_provenance_and_exact_results(
    setup: Setup,
) -> None:
    ma, t, _, _, s, _ = setup
    t.responses.append(
        native(
            steps=[
                {
                    "type": "code_execution_call",
                    "id": "code",
                    "arguments": {"language": "python", "code": "print(55)"},
                },
                {"type": "code_execution_result", "call_id": "code", "result": "55\n"},
                {
                    "type": "mcp_server_tool_call",
                    "id": "mcp",
                    "name": "sum",
                    "server_name": "math",
                    "arguments": {"x": 1},
                },
                {"type": "mcp_server_tool_result", "call_id": "mcp", "result": {"total": 1}},
            ]
        )
    )
    await ma.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    page = await ma.events.list(SCOPE, s.ref, page=PageRequest())
    calls = [e.payload for e in page.data if e.type == "agent.tool_use"]
    results = [e.typed_payload() for e in page.data if e.type == "agent.tool_result"]
    assert [(c["call_id"], c["executor"]) for c in calls] == [("code", "agent"), ("mcp", "mcp")]
    assert calls[1]["mcp_server"] == "math"
    assert isinstance(results[0], ToolResultPayload)
    assert results[0].content == (TextPart(text="55\n"),)
    assert isinstance(results[1], ToolResultPayload)
    rich = results[1].content[0]
    assert isinstance(rich, NativePart)
    assert rich.payload["result"] == {"total": 1}


@pytest.mark.asyncio
async def test_durable_acknowledgement_recovers_projection_after_commit_crash(setup: Setup) -> None:
    _, t, storage, state, s, _ = setup
    from mux.state.memory import SimulatedCrash

    crashing = state.restart(crash={"advance_operation": "after_commit"})
    first = GeminiManagedAgents(
        t, storage=storage, state_store=crashing, account_scope_id="project"
    )
    t.responses.append(native())
    with pytest.raises(SimulatedCrash):
        await first.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    restarted = GeminiManagedAgents(
        t, storage=storage, state_store=state.restart(), account_scope_id="project"
    )
    receipt = await restarted.events.send(SCOPE, s.ref, (MESSAGE,), key="first")
    assert receipt.status == "queued" and receipt.input_ids == ("i1",)
    page = await restarted.events.list(SCOPE, s.ref, page=PageRequest())
    assert [e.type for e in page.data].count("user.message") == 1
    assert [e.type for e in page.data].count("session.turn_ended") == 1
    assert len(t.requests) == 1


@pytest.mark.parametrize("tools", [None, ()])
def test_unconfigured_agent_compiles_an_explicit_empty_tools_list(
    tools: tuple[ToolSpec, ...] | None,
) -> None:
    spec = AgentSpec(
        name="bounded", model=ModelRef(provider="gemini", id="gemini-3.8-flash"), tools=tools
    )
    assert compile_agent(spec)["tools"] == []


def test_configured_agent_compiles_only_declared_builtin_custom_and_mcp_tools() -> None:
    spec = AgentSpec(
        name="bounded",
        model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
        tools=(
            ToolSpec(name="code_execution", kind="builtin"),
            ToolSpec(name="weather", kind="custom"),
        ),
        mcp_servers=(MCPConnection(name="owned", url="https://owned.example/mcp"),),
    )
    assert compile_agent(spec)["tools"] == [
        {"type": "code_execution"},
        {"type": "function", "name": "weather", "description": "", "parameters": {}},
        {"type": "mcp_server", "name": "owned", "url": "https://owned.example/mcp"},
    ]
