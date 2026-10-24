"""I3 native-wire host bindings; authored replay, never remote tool certification."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

import httpx
import pytest
import yaml
from daimon.core.mux_backend import TurnBackendRequest, turn_backend
from daimon.core.turn.driver import run_turn
from daimon.core.turn.openai_host import OpenAIHostRuntime
from daimon.core.turn.openai_state import OpenAIRecoveryJournal, OpenAIUsageRevisions
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.prepare import ProviderPreparationRequest
from daimon.core.turn.privacy import TurnPrivacy
from daimon.core.turn.state import TurnState, extract_final_response
from daimon.testing.discord_surface import DiscordSurfaceCapture, DiscordSurfaceHarness
from daimon.testing.outcome_oracle import (
    CardUpdate,
    OutcomeReport,
    PostHistoryEvidence,
    PostInputAcknowledgement,
    PostRootAcknowledgement,
    RunEvidence,
    SessionUse,
    TerminalEvidence,
    TurnEvidence,
    VisibleText,
    evaluate,
)
from mux.contracts.actions import UserMessage
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.events import Event, TextPart, TurnEndedPayload, UserMessagePayload
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import SendReceipt
from mux.contracts.resources import (
    AgentSpec,
    EnvironmentSpec,
    ProviderBinding,
    SessionSpec,
    ToolSpec,
)
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.transport import SDKTransport as GeminiSDKTransport
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.transport import SDKTransport as OpenAISDKTransport
from mux.state.memory import MemoryStateStore
from mux.state.operations import OperationRecord, request_digest
from mux.state.store import binding_slot
from mux.testing.drivers.anthropic.ma import send_events_response
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
from mux.testing.drivers.gemini.fake import MemoryStorage
from mux.testing.isolated_sdk import build_async_anthropic, build_async_openai, build_gemini_client
from pydantic import JsonValue, TypeAdapter

pytestmark = pytest.mark.usefixtures("gemini_recorded_clock")

Object = dict[str, JsonValue]
_OBJECT = TypeAdapter(Object)
Backend = Literal["anthropic", "openai", "gemini"]
SCENARIO = "QA-I3-TOOL-ONLY-NO-EMPTY-RESPONSE"
TEXT = "QA tool-only. Run `true` with bash. Do not write any reply text at all."
SCOPE = Scope(
    tenant_id="i3-tenant",
    account_id="i3-caller",
    principal_id="i3-host",
    authorization_id="offline",
)
CHANNEL = ChannelRef(tenant_id=SCOPE.tenant_id, platform="discord", channel_id="i3-channel")
THREAD = ThreadRef(channel=CHANNEL, thread_id="i3-thread")
_TLS = frozenset(
    {
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SSLKEYLOGFILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    }
)


def require_replay_environment() -> None:
    # Covers the shared factory's pending TLS-keylog delta before construction.
    if any(
        name.upper() in _TLS
        or name.upper().startswith(("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_"))
        for name in os.environ
    ):
        raise ValueError("I3 replay refuses ambient SDK/TLS/proxy configuration")


def object_(value: JsonValue) -> Object:
    return _OBJECT.validate_python(value, strict=True)


def text(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("native tape identity is missing")
    return value


@dataclass(frozen=True)
class Native:
    backend: Backend
    profile: str
    model: str
    session: str
    root: str
    request: Object
    frames: tuple[Object, ...]
    snapshot: Object


class Stream(httpx.AsyncByteStream):
    def __init__(self, frames: tuple[Object, ...], accepted: asyncio.Event) -> None:
        self.frames, self.accepted = frames, accepted
        self.closed = False
        self.consumed = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await self.accepted.wait()
        for frame in self.frames:
            self.consumed += 1
            yield ("data: " + json.dumps(frame) + "\n\n").encode()

    async def aclose(self) -> None:
        self.closed = True


class Wire:
    """Finite native HTTP replies, not a generic success/snapshot router."""

    def __init__(self, native: Native) -> None:
        self.native = native
        self.requests: list[httpx.Request] = []
        self.accepted = asyncio.Event()
        self.stream = Stream(native.frames, self.accepted)
        self.used: Counter[str] = Counter()
        self.limits = (
            {"send": 1, "stream": 1, "saved": 1}
            if native.backend == "gemini"
            else {"turns": 1, "items": 1, "session": 2, "send": 1, "stream": 1}
        )

    def take(self, name: str) -> None:
        self.used[name] += 1
        if self.used[name] > self.limits.get(name, 0):
            raise ValueError("native I3 request exceeds its finite tape")

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        n = self.native
        path = request.url.path
        base = f"/v1/agents/sessions/{n.session}"
        if n.backend == "gemini":
            base = f"/v1beta/interactions/{n.root}"
        if request.method == "POST" and path == (
            "/v1beta/interactions" if n.backend == "gemini" else base + "/events"
        ):
            self.take("send")
            if _OBJECT.validate_json(request.content) != n.request:
                raise ValueError("native submitted bytes differ from the frozen I3 request")
            self.accepted.set()
            return (
                httpx.Response(200, json={**n.snapshot, "status": "in_progress", "steps": []})
                if n.backend == "gemini"
                else httpx.Response(202, json={})
            )
        if (
            request.method == "GET"
            and path == (base if n.backend == "gemini" else base + "/events")
            and request.url.params.get("stream") == "true"
        ):
            self.take("stream")
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=self.stream
            )
        if request.method == "GET" and path == base:
            self.take("saved" if n.backend == "gemini" else "session")
            if n.backend == "gemini":
                return httpx.Response(200, json=n.snapshot)
            return httpx.Response(
                200,
                json={
                    "id": n.session,
                    "agent": {
                        "id": "qa-agent",
                        "model": n.model,
                        "multi_agent": {"enabled": False},
                    },
                    "created_at": 0,
                    "environment": {"type": "openai_hosted", "id": "qa-environment"},
                    "status": "idle",
                    "required_actions": [],
                    "metadata": {"mux_tenant": SCOPE.tenant_id},
                },
            )
        for kind in ("turns", "items"):
            if n.backend == "openai" and request.method == "GET" and path == base + "/" + kind:
                self.take(kind)
                return httpx.Response(200, json={"data": [], "has_more": False})
        raise ValueError("unscripted native I3 HTTP request")

    def finish(self) -> None:
        if (
            dict(self.used) != self.limits
            or not self.stream.closed
            or self.stream.consumed != len(self.native.frames)
        ):
            raise ValueError("native I3 wire was not consumed and closed exactly once")


@dataclass(frozen=True)
class RequestProof:
    method: str
    path: str
    body: bytes


@dataclass(frozen=True)
class Capture:
    evidence: TurnEvidence
    outcome: OutcomeReport
    surface: DiscordSurfaceCapture
    operation: Object
    journal: tuple[Event, ...]
    requests: tuple[RequestProof, ...]
    state: TurnState
    store: MemoryStateStore
    binding: ProviderBinding
    native: Native
    started: float
    observed: float
    card_id: str
    operation_key: str


async def bind_history(
    *,
    store: MemoryStateStore,
    binding: ProviderBinding,
    native: Native,
    surface: DiscordSurfaceCapture,
    started: float,
    observed: float,
    state: TurnState,
    card_id: str,
    operation_key: str,
) -> TurnEvidence:
    if state.error is not None or extract_final_response(state.content).strip():
        raise ValueError("native I3 host failed or fabricated an answer")
    operation = await store.get_operation(SCOPE, operation_key + ":send:0")
    if (
        operation is None
        or operation.operation.status not in ("accepted", "processed")
        or not operation.result
    ):
        raise ValueError("native I3 lacks the original accepted operation")
    receipt = SendReceipt.model_validate(operation.result)
    expected_key = ("driver:" if native.backend == "gemini" else "") + operation_key + ":send:0"
    inputs: list[JsonValue] = [
        _OBJECT.validate_python(UserMessage(content=(TextPart(text=TEXT),)).model_dump(mode="json"))
    ]
    request: JsonValue = {"events": inputs} if native.backend == "openai" else inputs
    expected_digest = request_digest(
        {"session": native.session, "kind": "send", "request": request}
    )
    if (
        operation.tenant_id != SCOPE.tenant_id
        or operation.account_id != SCOPE.account_id
        or operation.principal_id != SCOPE.principal_id
        or operation.slot != binding_slot(binding)
        or operation.operation.resource is None
        or operation.operation.resource
        != ResourceRef(
            id=native.session,
            kind="session",
            provider=native.backend,
            account_scope_id="i3-offline",
            tenant_id=SCOPE.tenant_id,
            account_id=SCOPE.account_id,
        )
        or operation.operation.request_digest != expected_digest
        or receipt.operation_id != expected_key
        or receipt.status not in ("queued", "processed")
    ):
        raise ValueError("native I3 acknowledgement differs from the submitted operation")
    journal = tuple(await store.read_events(native.session))
    roots = [e for e in journal if e.turn_id is not None]
    if not roots or any(
        e.turn_id != native.root
        or e.session_id != native.session
        or e.native.provider != native.backend
        for e in roots
    ):
        raise ValueError("native I3 journal contains an unowned root")
    terminals = tuple(
        TerminalEvidence(
            evidence_id="terminal:" + e.id,
            session_id=e.session_id,
            root_turn_id=native.root,
            authority=e.authority,
            outcome=TurnEndedPayload.model_validate(e.payload).outcome,
            observed_s=observed,
        )
        for e in roots
        if e.type == "session.turn_ended"
    )
    if not terminals or not all(
        t.outcome == "completed" and t.authority in ("record", "reconciled") for t in terminals
    ):
        raise ValueError("native I3 lacks a completed authoritative root")
    if any(e.type == "agent.message" for e in roots) or not {
        "agent.tool_use",
        "agent.tool_result",
    } <= {e.type for e in roots}:
        raise ValueError("native I3 tape is not a genuine tool-only turn")
    if (
        (surface.session_id, surface.root_turn_id) != (native.session, native.root)
        or not started <= surface.started_s <= surface.observed_s <= observed
        or not surface.post_capture_complete
    ):
        raise ValueError("native I3 post capture differs from its owned root window")
    # HTTP202 acknowledges the immutable send, not a native input/root ID.
    # Join its unchanged empty receipt to exactly one authoritative fresh input
    # record that echoes that send. Its ID is retained separately, never placed
    # in receipt.input_ids. The virgin baseline and accepted stream gate exclude
    # historical roots and pre-send input records.
    input_record = None
    if native.backend == "openai":
        messages = [e for e in roots if e.type == "user.message"]
        if len(messages) != 1 or messages[0].authority not in ("record", "reconciled"):
            raise ValueError("OpenAI I3 lacks a unique authoritative accepted input")
        message = messages[0]
        user = UserMessagePayload.model_validate(message.payload)
        observed_text = "".join(p.text for p in user.content if isinstance(p, TextPart))
        if (
            user.content != (TextPart(text=TEXT),)
            or user.mode != "new_turn"
            or observed_text != TEXT
            or message.native.event_id is None
        ):
            raise ValueError("OpenAI I3 input does not acknowledge its submitted text")
        input_record = PostInputAcknowledgement(
            evidence_id="input-record:" + message.id,
            input_id=user.input_id,
            session_id=message.session_id,
            root_turn_id=native.root,
            authority=message.authority,
            submitted_text=TEXT,
            observed_text=observed_text,
        )
    ack = PostRootAcknowledgement(
        evidence_id="ack:" + operation_key,
        session_id=native.session,
        root_turn_id=native.root,
        operation_id=receipt.operation_id,
        status=receipt.status,
        input_ids=receipt.input_ids,
        input_record=input_record,
    )
    if receipt.turn_id is not None and receipt.turn_id != native.root:
        raise ValueError("native I3 receipt belongs to a different root")
    if native.backend != "openai" and receipt.input_ids != (native.root,):
        raise ValueError("native I3 receipt input differs from the captured root")
    payload = surface.model_dump()
    payload["acknowledgement"] = ack.model_dump()
    history = PostHistoryEvidence.model_validate(payload)
    card_events = [
        e for e in surface.events if e.operation != "delete" and e.message.message_id == card_id
    ]
    card = next((m for m in surface.messages if m.message_id == card_id), None)
    if card is None or not card_events or not any("Done." in t for t in card.embed_texts):
        raise ValueError("native I3 lacks the actually accepted finalized card")
    texts = tuple(
        VisibleText(
            evidence_id=f"{surface.evidence_id}:survivor:{m.message_id}:{i}",
            message_id=m.message_id,
            text=t,
            order=1 + i,
            observed_s=surface.observed_s,
        )
        for i, (m, t) in enumerate(
            (m, t)
            for m in surface.messages
            for t in (m.content, *m.embed_texts, *m.component_labels)
            if t.strip()
        )
    )
    return TurnEvidence(
        turn=1,
        slot_id=binding_slot(binding).model_dump_json(),
        session_id=native.session,
        root_turn_id=native.root,
        started_s=started,
        terminals=terminals,
        sessions=(SessionUse(evidence_id="selected:" + binding.id, session_id=native.session),),
        terminal_capture_complete=not any(e.authority == "gap" for e in journal),
        session_capture_complete=True,
        texts=texts,
        text_capture_complete=surface.text_capture_complete,
        cards=(
            CardUpdate(
                evidence_id=surface.evidence_id + ":finalized-card",
                card_id=card_id,
                state="finalized",
                order=0,
                observed_s=card_events[-1].message.observed_s,
            ),
        ),
        card_capture_complete=True,
        post_history=history,
    )


async def execute(native: Native) -> Capture:
    require_replay_environment()
    revision = ConfigRevision.create(
        CHANNEL,
        1,
        resolve_default(
            BackendConfig(backend=native.backend, profile=native.profile, model=native.model)
        ),
    )
    store = MemoryStateStore()
    await store.put_config_revision(revision)
    session = ResourceRef(
        id=native.session,
        kind="session",
        provider=native.backend,
        account_scope_id="i3-offline",
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
    )
    binding = ProviderBinding(
        id="i3-binding",
        thread=THREAD,
        provider=native.backend,
        profile=native.profile,
        native_refs={"session": native.session},
        generation=1,
        config_revision=1,
        legacy_account_id=SCOPE.account_id,
    )
    operation_key = "i3-host-turn-1"
    sentinel_wire = ScriptedTransport()
    async with AsyncExitStack() as lifetime:
        sentinel = await lifetime.enter_async_context(
            build_async_anthropic(transport=httpx.MockTransport(sentinel_wire.dispatch))
        )
        gemini_sdk = None
        openai_sdk = None
        wire = None
        runtime = None
        if native.backend == "anthropic":
            sentinel_wire.queue(
                ScriptedReply.stream(
                    f"/v1/sessions/{native.session}/events/stream",
                    [dict[str, object](f) for f in native.frames],
                ),
                ScriptedReply(
                    "POST",
                    f"/v1/sessions/{native.session}/events",
                    send_events_response([native.frames[0]]),
                    request_json=native.request,
                    check_json=True,
                ),
            )
            request = TurnBackendRequest(
                profile=native.profile,
                client=sentinel,
                scope=SCOPE,
                session_id=session.id,
                session=session,
                config=revision,
                read_timeout_s=5,
            )
            backend: ManagedAgents = turn_backend(request).backend
        elif native.backend == "openai":
            wire = Wire(native)
            openai_sdk = await lifetime.enter_async_context(
                build_async_openai(transport=httpx.MockTransport(wire.handle))
            )
            transport = OpenAISDKTransport(openai_sdk)

            async def no_preparation(request: ProviderPreparationRequest) -> SessionSpec:
                raise ValueError("I3 replay cannot provision a provider session")

            def authorization(scope: Scope, kind: str, id_: str | None) -> bool:
                return scope == SCOPE and kind == "session" and id_ == native.session

            recovery = OpenAIRecoveryJournal()
            revisions = OpenAIUsageRevisions()
            controls = SessionControls(
                model="gpt-6-luna", multi_agent_enabled=False, container_size="small"
            )
            runtime = OpenAIHostRuntime(
                transport_factory=lambda config, scope: transport,
                journal=recovery,
                usage_store=revisions,
                account_scope_id=session.account_scope_id,
                session_plan=no_preparation,
                authorization=authorization,
                controls=controls,
            )
            backend = OpenAIDriver(
                transport,
                account_scope_id=session.account_scope_id,
                journal=recovery,
                usage_revisions=revisions,
                authorization=authorization,
                session_controls=controls,
            )
        else:
            wire = Wire(native)
            gemini_sdk = lifetime.enter_context(
                build_gemini_client(transport=httpx.MockTransport(wire.handle))
            )
            lifetime.push_async_callback(gemini_sdk.aio.aclose)
            storage = MemoryStorage()
            backend = GeminiManagedAgents(
                GeminiSDKTransport(gemini_sdk),
                storage=storage,
                state_store=store,
                account_scope_id=session.account_scope_id,
            )
            agent = await backend.agents.create(
                SCOPE,
                AgentSpec(
                    name="i3",
                    model=ModelRef(provider="gemini", id=native.model),
                    tools=(ToolSpec(name="code_execution", kind="builtin"),),
                ),
                key="i3-agent",
            )
            environment = await backend.environments.create(
                SCOPE, EnvironmentSpec(name="inline"), key="i3-env"
            )
            created = await backend.sessions.create(
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
                            value={
                                "binding_id": binding.id,
                                "thread": THREAD.model_dump(mode="json"),
                            },
                        )
                    },
                ),
                key="i3-session",
            )
            # Rename only a virgin local label; never seed journal/effects/usage.
            async with storage.transaction() as records:
                record = records.sessions.pop(created.ref.id)
                if record.current or record.events or record.observations:
                    raise ValueError("I3 bootstrap is not a virgin logical session")
                binding = created.binding.model_copy(
                    update={
                        "native_refs": {**created.binding.native_refs, "session": native.session},
                        "legacy_account_id": SCOPE.account_id,
                    }
                )
                record.session = created.model_copy(update={"ref": session, "binding": binding})
                records.sessions[native.session] = record
        try:
            request = TurnBackendRequest(
                profile=native.profile,
                client=sentinel,
                scope=SCOPE,
                session_id=session.id,
                session=session,
                config=revision,
                runtime=runtime,
                read_timeout_s=5,
            )
            await store.put_binding(binding, expected_generation=0)
            persistence = TurnPersistence(store, binding, SCOPE, operation_key=operation_key)
            started = time.monotonic()
            surface = DiscordSurfaceHarness(
                evidence_id="i3:" + native.backend + ":posts",
                session_id=native.session,
                root_turn_id=native.root,
                model_id=native.model,
            )
            state: TurnState | None = None

            async def invoke(lifecycle: object) -> None:
                nonlocal state
                await surface.lifecycle.post_initial()
                with persistence.activate():
                    state = await asyncio.wait_for(
                        run_turn(
                            privacy=TurnPrivacy("test"),
                            anthropic=sentinel,
                            session_id=native.session,
                            session_ref=session,
                            scope=SCOPE,
                            backend=backend,
                            backend_request=request,
                            profile=native.profile,
                            path="mux",
                            user_message=TEXT,
                            lifecycle=surface.lifecycle,
                            cancel=asyncio.Event(),
                            billing=BillingExempt(reason="headless-unrecorded"),
                            stream_read_timeout_s=5,
                            render_interval_s=3600,
                        ),
                        timeout=15,
                    )

            capture = await surface.run(invoke)
            observed = time.monotonic()
            if state is None or surface.lifecycle.message_ref is None:
                raise ValueError("I3 actual host/card capture is absent")
            evidence = await bind_history(
                store=store,
                binding=binding,
                native=native,
                surface=capture,
                started=started,
                observed=observed,
                state=state,
                card_id=str(surface.lifecycle.message_ref.id),
                operation_key=operation_key,
            )
            if wire is not None:
                wire.finish()
            if (
                sentinel_wire.replies
                or sentinel_wire.violations
                or native.backend != "anthropic"
                and sentinel_wire.requests
            ):
                raise ValueError("I3 crossed the wrong provider or retained native requests")
            assertions = i3_assertions()
            outcome = evaluate(
                RunEvidence(scenario_id=SCENARIO, backend=native.backend, turns=(evidence,)),
                assertions,
            )
            operation = await store.get_operation(SCOPE, operation_key + ":send:0")
            if operation is None:
                raise ValueError("I3 lost the original operation")
            return Capture(
                evidence,
                outcome,
                capture,
                _OBJECT.validate_python(operation.model_dump(mode="json")),
                tuple(await store.read_events(native.session)),
                tuple(RequestProof(r.method, r.url.path, r.content) for r in wire.requests)
                if wire is not None
                else tuple(RequestProof(r.method, r.path, r.body) for r in sentinel_wire.requests),
                state,
                store,
                binding,
                native,
                started,
                observed,
                str(surface.lifecycle.message_ref.id),
                operation_key,
            )
        finally:
            if gemini_sdk is not None:
                await gemini_sdk.aio.aclose()
                gemini_sdk.close()
            if openai_sdk is not None:
                await openai_sdk.close()


ROOT = Path(__file__).resolve().parents[4]
FIXTURES = Path(__file__).parent / "fixtures"


def i3_assertions() -> list[Object]:
    raw = (FIXTURES / "i3-scenario.yaml").read_bytes()
    proof = _OBJECT.validate_json((FIXTURES / "i3-source-proof.json").read_bytes())
    if hashlib.sha256(raw).hexdigest() != proof["source_sha256"]:
        raise ValueError("original I3 scenario bytes changed")
    scenario = object_(yaml.safe_load(raw))
    if scenario["id"] != SCENARIO:
        raise ValueError("I3 scenario identity changed")
    assertions = scenario["assert"]
    if not isinstance(assertions, list):
        raise ValueError("original I3 assertions are missing")
    return [object_(a) for a in assertions]


def load(backend: str) -> Native:
    proof = json.loads((FIXTURES / "i3-source-proof.json").read_bytes())
    pin = proof[backend]
    path = ROOT / pin["path"] if backend == "anthropic" else FIXTURES / pin["path"]
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != pin["sha256"]:
        raise ValueError("recorded I3 source bytes changed")
    document = json.loads(raw)
    if backend == "anthropic":
        fixture = next(d for d in document if d["scenario_id"] == SCENARIO)
        turn = fixture["turns"][0]
        if turn["request_text"] != TEXT or fixture["scenario_sha256"] != proof["source_sha256"]:
            raise ValueError("Anthropic I3 source identity changed")
        return Native(
            "anthropic",
            "anthropic.managed_agents",
            "claude-haiku-5-5",
            "i3-anthropic-session",
            turn["events"][0]["id"],
            {"events": [{"type": "user.message", "content": [{"type": "text", "text": TEXT}]}]},
            tuple(object_(f) for f in turn["events"]),
            {},
        )
    if backend == "openai":
        if (
            document["scenario_id"] != SCENARIO
            or document["source"]["sha256"] != proof["source_sha256"]
        ):
            raise ValueError("OpenAI I3 source identity changed")
        fixture = next(d for d in document["providers"] if d["backend"] == backend)
        turn = fixture["turns"][0]
        if turn["request_text"] != TEXT:
            raise ValueError("OpenAI I3 native input changed")
        return Native(
            "openai",
            fixture["profile"],
            fixture["model"],
            turn["session_id"],
            turn["root_id"],
            object_(turn["request"]),
            tuple(object_(f) for f in turn["frames"]),
            object_(turn["snapshot"]),
        )
    if (
        document["scenario_id"] != SCENARIO
        or document["source"]["sha256"] != proof["source_sha256"]
    ):
        raise ValueError("Gemini I3 source identity changed")
    return Native(
        "gemini",
        text(document["profile"]),
        text(document["model"]),
        text(document["session"]),
        text(document["root"]),
        object_(document["request"]),
        tuple(object_(f) for f in document["frames"]),
        object_(document["snapshot"]),
    )


@pytest.mark.parametrize("backend", ["gemini", "anthropic", "openai"])
async def test_i3_native_tool_only_host_cells(backend: str, tmp_path: Path) -> None:
    capture = await execute(load(backend))
    print(
        backend,
        capture.outcome.status,
        [(c.kind, c.status, c.code) for c in capture.outcome.checks],
    )
    assert capture.state.error is None
    assert all(c.status == "PASS" for c in capture.outcome.checks if c.kind != "no_blank_message")
    assert capture.surface.post_capture_complete
    assert any(e.operation == "send" for e in capture.surface.events)
    assert any(e.operation == "edit" for e in capture.surface.events)
    assert all(not m.content.strip() for m in capture.surface.messages)
    assert not any(e.type == "agent.message" for e in capture.journal)
    assert capture.outcome.status == "PASS"
    if backend == "openai":
        assert object_(capture.operation["result"])["input_ids"] == []
    output = tmp_path / ("i3-" + backend + "-capture.json")
    print("I3_CAPTURE", backend, output)
    output.write_text(
        json.dumps(
            {
                "backend": backend,
                "status": capture.outcome.status,
                "outcome": capture.outcome.model_dump(mode="json"),
                "evidence": capture.evidence.model_dump(mode="json"),
                "operation": capture.operation,
                "journal": [e.model_dump(mode="json") for e in capture.journal],
                "surface": capture.surface.model_dump(mode="json"),
                "requests": [
                    {"method": r.method, "path": r.path, "body": r.body.decode()}
                    for r in capture.requests
                ],
            },
            indent=2,
        )
        + "\n"
    )


@pytest.mark.parametrize("backend", ["gemini", "anthropic", "openai"])
async def test_native_capture_cannot_certify_foreign_acknowledgement(backend: str) -> None:
    capture = await execute(load(backend))
    history = capture.evidence.post_history
    assert history is not None and history.acknowledgement is not None
    for target in ("session_id", "root_turn_id"):
        wrong = history.acknowledgement.model_copy(update={target: "foreign"})
        evidence = capture.evidence.model_copy(
            update={"post_history": history.model_copy(update={"acknowledgement": wrong})}
        )
        result = evaluate(
            RunEvidence(scenario_id=SCENARIO, backend=capture.native.backend, turns=(evidence,)),
            [{"kind": "no_blank_message", "turn": 1}],
        )
        assert result.status == "FAIL"


@pytest.mark.parametrize("bad", ["input_text", "input_root", "input_session", "preview", "gap"])
async def test_openai_native_input_acknowledgement_cannot_be_forged(bad: str) -> None:
    capture = await execute(load("openai"))
    history = capture.evidence.post_history
    assert history is not None and history.acknowledgement is not None
    ack = history.acknowledgement
    assert ack.input_ids == () and ack.input_record is not None
    field, value = {
        "input_text": ("observed_text", "foreign input"),
        "input_root": ("root_turn_id", "foreign"),
        "input_session": ("session_id", "foreign"),
        "preview": ("authority", "preview"),
        "gap": ("authority", "gap"),
    }[bad]
    wrong = ack.input_record.model_copy(update={field: value})
    evidence = capture.evidence.model_copy(
        update={
            "post_history": history.model_copy(
                update={"acknowledgement": ack.model_copy(update={"input_record": wrong})}
            )
        }
    )
    result = evaluate(
        RunEvidence(scenario_id=SCENARIO, backend="openai", turns=(evidence,)),
        [{"kind": "no_blank_message", "turn": 1}],
    )
    assert result.status == "FAIL"


@pytest.mark.parametrize("bad", ["request_digest", "resource", "slot", "receipt_key", "unknown"])
async def test_binding_requires_original_owned_send_operation(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    capture = await execute(load("gemini"))
    original = capture.store.get_operation

    async def corrupted(scope: Scope, key: str) -> OperationRecord | None:
        record = await original(scope, key)
        assert record is not None
        if bad == "request_digest":
            return record.model_copy(
                update={
                    "operation": record.operation.model_copy(update={"request_digest": "foreign"})
                }
            )
        if bad == "resource":
            resource = record.operation.resource
            assert resource is not None
            return record.model_copy(
                update={
                    "operation": record.operation.model_copy(
                        update={"resource": resource.model_copy(update={"id": "foreign"})}
                    )
                }
            )
        if bad == "slot":
            assert record.slot is not None
            return record.model_copy(
                update={"slot": record.slot.model_copy(update={"account_id": "foreign"})}
            )
        assert record.result is not None
        result: Object = dict(record.result)
        result["operation_id" if bad == "receipt_key" else "status"] = (
            "foreign" if bad == "receipt_key" else "outcome_unknown"
        )
        return record.model_copy(update={"result": result})

    with monkeypatch.context() as patch:
        patch.setattr(capture.store, "get_operation", corrupted)
        with pytest.raises(ValueError, match="acknowledgement differs"):
            await bind_history(
                store=capture.store,
                binding=capture.binding,
                native=capture.native,
                surface=capture.surface,
                started=capture.started,
                observed=capture.observed,
                state=capture.state,
                card_id=capture.card_id,
                operation_key=capture.operation_key,
            )


@pytest.mark.parametrize(
    "name",
    [
        "SSLKEYLOGFILE",
        "https_proxy",
        "GOOGLE_API_KEY",
        "gemini_future_secret",
        "openai_private_setting",
        "ANTHROPIC_AUTH_TOKEN",
    ],
)
async def test_native_replay_names_only_preflight_precedes_sdk_allocation(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from collections.abc import Iterator, Mapping

    class Trap(Mapping[str, str]):
        def __iter__(self) -> Iterator[str]:
            return iter((name,))

        def __len__(self) -> int:
            return 1

        def __getitem__(self, key: str) -> str:
            raise AssertionError("present environment value was read")

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("SDK was allocated before refusal")

    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", Trap())
        patch.setattr(sys.modules[__name__], "build_async_anthropic", forbidden)
        patch.setattr(sys.modules[__name__], "build_async_openai", forbidden)
        patch.setattr(sys.modules[__name__], "build_gemini_client", forbidden)
        with pytest.raises(ValueError, match="refuses ambient SDK/TLS/proxy"):
            await execute(load("gemini"))
