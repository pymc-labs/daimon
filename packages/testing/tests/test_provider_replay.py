"""Actual SDK/port execution, missing capture and adversarial tape controls."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import httpx
import pytest
from daimon.testing.provider_fixtures import (
    BackendFixture,
    FixturePack,
    NativeTurn,
    ScenarioFixture,
    load_provider_fixtures,
    scenario_tape,
    turn_tape,
)
from daimon.testing.provider_replay import (
    Backend,
    Object,
    ProviderTape,
    SourcePin,
    WireFrame,
    WireReplay,
    WireReply,
    provider_replay,
)
from mux.contracts.actions import UserMessage
from mux.contracts.events import (
    TextPart,
    ToolResultPayload,
    ToolUsePayload,
    TurnEndedPayload,
)
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import AgentSpec, EnvironmentSpec, SessionSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.fake import MemoryStorage
from mux.drivers.gemini.transport import SDKTransport as GeminiTransport
from mux.drivers.gemini.transport import close_iterator
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport as OpenAITransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError
from mux.state.memory import MemoryStateStore
from pydantic import JsonValue, ValidationError

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "packages/testing/fixtures/target53"
PACK = load_provider_fixtures(DATA / "index.json", catalog_root=DATA / "catalog")
SCOPE = Scope(
    tenant_id="qa-tenant", account_id="account", principal_id="user", authorization_id="qa"
)
CASES = [(s, p, t) for s in PACK.scenarios for p in s.providers for t in p.turns]


def ref(backend: Backend, id_: str) -> ResourceRef:
    return ResourceRef(
        id=id_,
        kind="session",
        provider=backend,
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
        account_scope_id="project",
    )


def updated(tape: ProviderTape, **changes: JsonValue) -> ProviderTape:
    return ProviderTape.model_validate({**tape.model_dump(mode="json"), **changes})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "fixture", "turn"),
    CASES,
    ids=[f"{s.scenario_id}:{p.backend}:{t.turn}" for s, p, t in CASES],
)
async def test_every_authored_native_turn_traverses_actual_sdk_and_port(
    scenario: ScenarioFixture,
    fixture: BackendFixture,
    turn: NativeTurn,
) -> None:
    tape = turn_tape(scenario, fixture, turn, key="qa-send")
    async with provider_replay(
        tape,
        backend=fixture.backend,
        profile=fixture.profile,
        model=fixture.model,
        source_root=DATA / "catalog",
    ) as replay:
        message = UserMessage(content=(TextPart(text=turn.request_text),))
        if isinstance(replay.transport, OpenAITransport):
            driver = OpenAIDriver(
                replay.transport,
                account_scope_id="project",
                journal=MemoryRecoveryJournal(),
                usage_revisions=MemoryUsageRevisions(),
                authorization=lambda scope, kind, identity: (
                    scope == SCOPE and kind == "session" and identity == turn.session_id
                ),
            )
            session = ref("openai", turn.session_id)
            # Real G1 may open its stream before the POST; no frame may race acceptance.
            stream = await driver.events.open_stream(SCOPE, session)
            receipt = await driver.events.send(SCOPE, session, (message,), key="qa-send")
            assert receipt.status == "queued"
            if turn.completion_gate:
                replay.wire.release(turn.completion_gate)
            events = [e async for e in stream]
            await close_iterator(stream)
        else:
            assert isinstance(replay.transport, GeminiTransport)
            # A follow-up's POST is tested below through a retained driver/binding.
            # Here validate its unchanged exact wire body using the real SDK edge.
            if "previous_interaction_id" in turn.request:
                created = await replay.transport.create(turn.request)
                assert created["id"] == turn.root_id
                stream = await replay.transport.open_stream(turn.root_id)
                frames = [f async for f in stream]
                await close_iterator(stream)
                assert frames == list(turn.frames)
                saved = await replay.transport.get(turn.root_id)
                assert saved["steps"] == turn.snapshot["steps"]
                from datetime import UTC, datetime

                from mux.drivers.gemini.normalize import saved_events

                events = list(
                    saved_events(
                        saved,
                        ref("gemini", turn.session_id),
                        root=turn.root_id,
                        now=datetime.now(UTC),
                    )
                )
            else:
                driver_g = GeminiManagedAgents(
                    replay.transport,
                    storage=MemoryStorage(),
                    state_store=MemoryStateStore(),
                    account_scope_id="project",
                )
                agent = await driver_g.agents.create(
                    SCOPE,
                    AgentSpec(name="qa-agent", model=ModelRef(provider="gemini", id=fixture.model)),
                    key="agent",
                )
                env = await driver_g.environments.create(
                    SCOPE, EnvironmentSpec(name="qa-env"), key="env"
                )
                thread = ThreadRef(
                    channel=ChannelRef(
                        tenant_id=SCOPE.tenant_id, platform="headless", channel_id=turn.channel
                    ),
                    thread_id=turn.thread,
                )
                spec = SessionSpec(
                    agent=agent.ref,
                    agent_revision=agent.revision,
                    environment=env.ref,
                    config_revision=1,
                    extensions={
                        "gemini.session": ExtensionConfig(
                            namespace="gemini.session",
                            version=1,
                            value={
                                "thread": thread.model_dump(mode="json"),
                                "binding_id": "qa-binding",
                            },
                        )
                    },
                )
                session_g = await driver_g.sessions.create(SCOPE, spec, key="session")
                receipt = await driver_g.events.send(
                    SCOPE, session_g.ref, (message,), key="qa-send"
                )
                assert receipt.turn_id == turn.root_id
                if turn.completion_gate:
                    replay.wire.release(turn.completion_gate)
                stream_g = await driver_g.events.open_stream(SCOPE, session_g.ref)
                events = [e async for e in stream_g]
                await close_iterator(stream_g)
        replay.wire.assert_consumed()
        terminals = [e.typed_payload() for e in events if e.type == "session.turn_ended"]
        assert len(terminals) == 1
        terminal = terminals[0]
        assert isinstance(terminal, TurnEndedPayload)
        assert terminal.root_turn_id == turn.root_id and terminal.outcome == "completed"
        calls = [e.typed_payload() for e in events if e.type == "agent.tool_use"]
        results = [e.typed_payload() for e in events if e.type == "agent.tool_result"]
        assert {p.call_id for p in calls if isinstance(p, ToolUsePayload)} == {
            p.call_id for p in results if isinstance(p, ToolResultPayload)
        }
        assert all(e.native is not None for e in events)
        if scenario.scenario_id == "QA-I3-TOOL-ONLY-NO-EMPTY-RESPONSE":
            assert calls and results
            assert not any(e.type == "agent.message" for e in events)


def simple_tape(tmp_path: Path, backend: Backend = "openai") -> ProviderTape:
    path = tmp_path / "scenario.yaml"
    path.write_bytes(b"offline source\n")
    return ProviderTape(
        scenario_id="test",
        backend=backend,
        profile="openai.persistent_workspace" if backend == "openai" else "gemini.inline_reuse",
        model="gpt-6-luna" if backend == "openai" else "gemini-3.8-flash",
        api_family="agents-sessions" if backend == "openai" else "interactions",
        sdk_distribution="openai" if backend == "openai" else "google-genai",
        sdk_version="2.54.0" if backend == "openai" else "2.7.0",
        source=SourcePin(path=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest()),
        replies=(
            WireReply(
                id="one",
                method="POST",
                path="/v1/example",
                request_json={"text": "hello"},
                headers=(("openai-beta", "agents=v1"),),
                response_json={"accepted": True},
            ),
        ),
    )


@pytest.mark.parametrize("mutation", ["body", "query", "method", "path", "header"])
def test_wrong_request_never_consumes_or_disappears(tmp_path: Path, mutation: str) -> None:
    replay = WireReplay(simple_tape(tmp_path))
    request = httpx.Request(
        "DELETE" if mutation == "method" else "POST",
        "https://offline.invalid/v1/wrong"
        if mutation == "path"
        else "https://offline.invalid/v1/example?unexpected=1"
        if mutation == "query"
        else "https://offline.invalid/v1/example",
        json={"text": "foreign" if mutation == "body" else "hello"},
        headers={"OpenAI-Beta": "wrong" if mutation == "header" else "agents=v1"},
    )
    with pytest.raises(AssertionError, match="mismatched"):
        replay.dispatch(request)
    replay.dispatch(
        httpx.Request(
            "POST",
            "https://offline.invalid/v1/example",
            json={"text": "hello"},
            headers={"OpenAI-Beta": "agents=v1"},
        )
    )
    with pytest.raises(AssertionError, match="recorded violations"):
        replay.assert_consumed()


@pytest.mark.asyncio
async def test_sdk_wrapped_violations_remain_and_do_not_retry(tmp_path: Path) -> None:
    tape = simple_tape(tmp_path)
    async with provider_replay(
        tape, backend="openai", profile=tape.profile, model=tape.model, source_root=tmp_path
    ) as replay:
        assert isinstance(replay.transport, OpenAITransport)
        with pytest.raises(ProviderError):
            await replay.transport.request("POST", "/example", body={"text": "wrong"})
        assert len(replay.wire.requests) == 1
        with pytest.raises(AssertionError, match="recorded violations"):
            replay.wire.assert_consumed()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile", "openai.conversation_only"),
        ("model", "gpt-6-sol"),
        ("api_family", "responses"),
        ("sdk_version", "2.53.0"),
        ("backend", "gemini"),
    ],
)
def test_foreign_protocol_selection_is_refused(tmp_path: Path, field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        updated(simple_tape(tmp_path), **{field: value})


@pytest.mark.asyncio
async def test_factory_rejects_source_mutation_and_persisted_backend_mismatch(
    tmp_path: Path,
) -> None:
    tape = simple_tape(tmp_path)
    with pytest.raises(ValueError, match="persisted"):
        async with provider_replay(
            tape, backend="gemini", profile=tape.profile, model=tape.model, source_root=tmp_path
        ):
            pytest.fail("foreign factory opened")
    (tmp_path / tape.source.path).write_text("changed")
    with pytest.raises(ValueError, match="source pin changed"):
        async with provider_replay(
            tape, backend="openai", profile=tape.profile, model=tape.model, source_root=tmp_path
        ):
            pytest.fail("mutated source opened")


@pytest.mark.asyncio
async def test_stream_open_is_not_consumption_and_frames_wait_for_accepted_post(
    tmp_path: Path,
) -> None:
    original = simple_tape(tmp_path)
    frame: Object = {"type": "agent.session.turn.completed", "event_id": "end"}
    stream_reply = WireReply(
        id="stream",
        method="GET",
        path="/v1/example",
        query=(("stream", "true"),),
        frames=(WireFrame(payload=frame, after_gate="accepted"),),
        hold_open=True,
    )
    send = original.replies[0].model_copy(update={"releases": ("accepted",)})
    tape = original.model_copy(update={"replies": (send, stream_reply)})
    async with provider_replay(
        tape, backend="openai", profile=tape.profile, model=tape.model, source_root=tmp_path
    ) as replay:
        assert isinstance(replay.transport, OpenAITransport)
        stream = await replay.transport.open_stream("/example")
        with pytest.raises(AssertionError, match="unconsumed provider replies"):
            replay.wire.assert_consumed()
        await replay.transport.request("POST", "/example", body={"text": "hello"})
        with pytest.raises(AssertionError, match="unconsumed native SSE"):
            replay.wire.assert_consumed()
        assert await anext(stream) == frame
        with pytest.raises(AssertionError, match="not closed"):
            replay.wire.assert_consumed()
        await close_iterator(stream)
        replay.wire.assert_consumed()


def test_source_traversal_and_unmatched_secret_headers_are_not_recorded(tmp_path: Path) -> None:
    tape = simple_tape(tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        SourcePin(path="../secret", sha256=tape.source.sha256).verify(tmp_path)
    wire = WireReplay(tape)
    wire.dispatch(
        httpx.Request(
            "POST",
            "https://offline.invalid/v1/example",
            json={"text": "hello"},
            headers={"openai-beta": "agents=v1", "Authorization": "secret", "Cookie": "secret"},
        )
    )
    assert "secret" not in repr(wire.requests[0].protocol_headers)


def test_pack_keeps_all_53_sources_context_numbering_and_typed_gaps() -> None:
    assert len(PACK.scenarios) == 53
    assert sum(len(p.turns) for s in PACK.scenarios for p in s.providers) == 64
    scenario, native = PACK.select("QA-I6-BARE-MENTION-THREAD-TITLE", "openai")
    assert [t.turn for t in native.turns] == [2]
    assert scenario.invocations[1]["operation"] == "context"
    blocked, native = PACK.select("QA-NEW16-TENANT-CREDIT-DEPLETED-COPY", "gemini")
    assert not native.turns and any(g.code == "HOST_GATE" for g in native.gaps)
    assert blocked.scenario["setup"]
    manual, native = PACK.select("QA-NEW30-APPROVE-AFTER-RESTART", "openai")
    assert manual.scenario["human"] and not native.turns
    assert any(g.code == "MANUAL_TRIGGER" and g.status == "BLOCKED" for g in native.gaps)
    assert not hasattr(PACK, "outcome") and not hasattr(native, "verdict")
    assert sum(s.anthropic is not None for s in PACK.scenarios) == 5


def test_manifest_cannot_drop_or_invent_a_trigger() -> None:
    raw = PACK.model_dump(mode="json")
    scenarios = cast(list[dict[str, JsonValue]], raw["scenarios"])
    providers = cast(list[dict[str, JsonValue]], scenarios[0]["providers"])
    providers[0]["turns"] = []
    with pytest.raises(ValidationError, match="silently omitted"):
        FixturePack.model_validate(raw)


def test_embedded_source_and_attached_fixture_hashes_are_verified(tmp_path: Path) -> None:
    import shutil

    shutil.copytree(DATA / "catalog", tmp_path / "catalog")
    scenario = next(s for s in PACK.scenarios if s.assets)
    (tmp_path / "catalog" / scenario.assets[0].path).write_bytes(b"wrong fixture")
    with pytest.raises(ValueError, match="source pin changed"):
        PACK.verify_catalog(tmp_path / "catalog")


def test_public_projection_keeps_execution_fields_and_original_source_pin() -> None:
    for scenario in PACK.scenarios:
        assert "sources" not in scenario.scenario
        assert scenario.source.path == scenario.projection.path
        scenario.projection.verify(DATA / "catalog")


def test_catalog_projection_cannot_mask_changed_execution_fields(tmp_path: Path) -> None:
    import shutil

    shutil.copytree(DATA / "catalog", tmp_path / "catalog")
    scenario = PACK.scenarios[0]
    path = tmp_path / "catalog" / scenario.projection.path
    path.write_text(path.read_text() + "\nextra_execution_field: changed\n")
    with pytest.raises(ValueError, match="source pin changed"):
        PACK.verify_catalog(tmp_path / "catalog")


@pytest.mark.asyncio
async def test_gemini_two_turns_keep_real_binding_history_and_exact_post_count() -> None:
    scenario, fixture = PACK.select("QA-D1-CANARY-TWO-TURN", "gemini")
    tape = scenario_tape(scenario, fixture, keys={1: "one", 2: "two"})
    tape = tape.model_copy(
        update={
            "replies": (
                *tape.replies,
                WireReply(
                    id="verify-session",
                    method="GET",
                    path=f"/v1beta/interactions/{fixture.turns[-1].root_id}",
                    headers=(("api-revision", "2026-05-20"),),
                    response_json=fixture.turns[-1].snapshot,
                    after=("turn.2.saved",),
                ),
            )
        }
    )
    async with provider_replay(
        tape,
        backend="gemini",
        profile=fixture.profile,
        model=fixture.model,
        source_root=DATA / "catalog",
    ) as replay:
        assert isinstance(replay.transport, GeminiTransport)
        driver = GeminiManagedAgents(
            replay.transport,
            storage=MemoryStorage(),
            state_store=MemoryStateStore(),
            account_scope_id="project",
        )
        agent = await driver.agents.create(
            SCOPE,
            AgentSpec(name="qa", model=ModelRef(provider="gemini", id=fixture.model)),
            key="agent",
        )
        env = await driver.environments.create(SCOPE, EnvironmentSpec(name="qa"), key="env")
        thread = ThreadRef(
            channel=ChannelRef(
                tenant_id=SCOPE.tenant_id, platform="headless", channel_id="channel"
            ),
            thread_id="thread",
        )
        session = await driver.sessions.create(
            SCOPE,
            SessionSpec(
                agent=agent.ref,
                agent_revision=agent.revision,
                environment=env.ref,
                config_revision=1,
                extensions={
                    "gemini.session": ExtensionConfig(
                        namespace="gemini.session",
                        version=1,
                        value={"thread": thread.model_dump(mode="json"), "binding_id": "binding"},
                    )
                },
            ),
            key="session",
        )
        for index, turn in enumerate(fixture.turns):
            receipt = await driver.events.send(
                SCOPE,
                session.ref,
                (UserMessage(content=(TextPart(text=turn.request_text),)),),
                key="one" if index == 0 else "two",
            )
            assert receipt.turn_id == turn.root_id
            stream = await driver.events.open_stream(SCOPE, session.ref)
            events = [e async for e in stream]
            await close_iterator(stream)
            assert any(e.type == "session.turn_ended" and e.turn_id == turn.root_id for e in events)
        posts = [r.json() for r in replay.wire.requests if r.method == "POST"]
        assert len(posts) == 2
        assert isinstance(posts[1], dict)
        assert posts[1]["previous_interaction_id"] == fixture.turns[0].root_id
        assert posts[1]["environment"] == fixture.turns[0].snapshot["environment_id"]
        current = await driver.sessions.retrieve(SCOPE, session.ref)
        assert current.ref == session.ref and current.binding.id == session.binding.id
        replay.wire.assert_consumed()


@pytest.mark.asyncio
async def test_explicit_delayed_frame_gate_keeps_tool_completion_blocked() -> None:
    scenario, fixture = PACK.select("QA-D6-LONG-TURN-CARD-LIFECYCLE", "openai")
    turn = fixture.turns[0]
    tape = turn_tape(scenario, fixture, turn, key="send")
    async with provider_replay(
        tape,
        backend="openai",
        profile=fixture.profile,
        model=fixture.model,
        source_root=DATA / "catalog",
    ) as replay:
        assert isinstance(replay.transport, OpenAITransport)
        stream = await replay.transport.open_stream(f"/agents/sessions/{turn.session_id}/events")
        await replay.transport.request("GET", f"/agents/sessions/{turn.session_id}")
        await replay.transport.request(
            "POST", f"/agents/sessions/{turn.session_id}/events", body=turn.request, key="send"
        )
        assert (await anext(stream))["type"] == "agent.session.turn.in_progress"
        assert (await anext(stream))["item"] == turn.frames[1]["item"]
        assert turn.completion_gate is not None
        assert not replay.wire.gate(turn.completion_gate).is_set()
        replay.wire.release(turn.completion_gate)
        assert [f async for f in stream] == list(turn.frames[2:])
        await close_iterator(stream)
        replay.wire.assert_consumed()


def test_native_blob_pin_rejects_mutation(tmp_path: Path) -> None:
    import shutil

    shutil.copytree(DATA, tmp_path / "fixtures")
    blob = tmp_path / "fixtures/native" / f"{PACK.scenarios[0].scenario_id}.json"
    blob.write_text("{}")
    with pytest.raises(ValueError, match="source pin changed"):
        load_provider_fixtures(
            tmp_path / "fixtures/index.json", catalog_root=tmp_path / "fixtures/catalog"
        )
