"""Offline explicit-channel OpenAI preparation and actual fenced host turns."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from daimon.core import channel_backend
from daimon.core.turn import openai_host
from daimon.core.turn.openai_host import OpenAIHostRuntime, prepare_openai, runtime_for
from daimon.core.turn.openai_state import OpenAIRecoveryJournal, OpenAIUsageRevisions
from daimon.core.turn.persistence import TurnPersistence, UncertainSend
from daimon.core.turn.prepare import ProviderPreparationRequest
from daimon.core.usage_billing import ObservationRecorder
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.ids import PageRequest, ResourceRef, Revision, ThreadRef
from mux.contracts.resources import SessionSpec
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.transport import SDKTransport
from mux.errors import ContinuityLost, OperationConflict, ScopeViolation, UnsupportedCapability
from mux.state.lease import Slot
from mux.state.memory import MemoryStateStore
from openai import AsyncOpenAI

from .test_provider_dispatch import ACCOUNT, PROFILE, REVISION, SCOPE, TENANT, admission, deps_for

SESSION_ID = "native-openai-session"


class Stream(httpx.AsyncByteStream):
    def __init__(self, wire: Wire) -> None:
        self.wire = wire
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await self.wire.sent.wait()
        root = self.wire.turns[-1]
        for event in (
            {
                "type": "agent.session.turn.in_progress",
                "event_id": root["id"] + ":running",
                "turn": {**root, "status": "in_progress"},
            },
            {
                "type": "agent.session.turn.item.done",
                "event_id": root["id"] + ":answer",
                "turn_id": root["id"],
                "item": self.wire.items[-1],
            },
            {
                "type": "agent.session.turn.completed",
                "event_id": root["id"] + ":done",
                "turn": {**root, "status": "completed"},
            },
        ):
            event["session_id"] = SESSION_ID
            yield ("data: " + json.dumps(event) + "\n\n").encode()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


class Wire:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.turns: list[dict] = []
        self.items: list[dict] = []
        self.sent = asyncio.Event()
        self.sources: list[Stream] = []
        self.fail_create = False
        self.bad_model = False
        self.cancelled = False
        self.saved_delegation = False
        self.session_delegation = False

    def session(self):
        return {
            "id": SESSION_ID,
            "agent": {
                "id": "native-agent",
                "model": "different" if self.bad_model else "gpt-6-luna",
                "multi_agent": {"enabled": self.session_delegation},
            },
            "environment": {"type": "openai_hosted", "id": "native-environment"},
            "metadata": {"mux_tenant": str(TENANT)},
            "status": "idle",
            "created_at": 0,
        }

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.headers["OpenAI-Beta"] == "agents=v1"
        path = req.url.path.removeprefix("/v1")
        if path == "/agents/native-agent":
            return httpx.Response(
                200,
                json={
                    "id": "native-agent",
                    "metadata": {"mux_tenant": str(TENANT)},
                    "multi_agent": {"enabled": self.saved_delegation},
                },
            )
        if path == "/agents/sessions" and req.method == "POST":
            assert req.headers["Idempotency-Key"].startswith("openai:prepare:")
            body = json.loads(req.content)
            assert body["agent"] == {"model": "gpt-6-luna", "multi_agent": {"enabled": False}}
            assert body["environment"]["container_size"] == "small"
            assert body["spend_control"] == {"limit": 4}
            if self.fail_create:
                raise httpx.ReadError("offline lost acknowledgment")
            return httpx.Response(200, json=self.session())
        assert path.startswith("/agents/sessions/" + SESSION_ID)
        if path.endswith("/events"):
            if req.method == "GET":
                assert req.url.params["stream"] == "true"
                assert req.headers["Accept"] == "text/event-stream"
                self.sent = asyncio.Event()
                stream = Stream(self)
                self.sources.append(stream)
                return httpx.Response(
                    200, stream=stream, headers={"Content-Type": "text/event-stream"}
                )
            assert (
                "send" in req.headers["Idempotency-Key"]
                or "cancel" in req.headers["Idempotency-Key"]
            )
            body = json.loads(req.content)
            if body["events"][0]["type"] == "agent.session.input.cancel":
                self.cancelled = True
            else:
                root = f"turn-{len(self.turns) + 1}"
                text = body["events"][0]["input"][0]["content"][0]["text"]
                self.turns.append(
                    {
                        "id": root,
                        "session_id": SESSION_ID,
                        "agent_id": "native-agent",
                        "subagent_id": None,
                        "status": "completed",
                        "created_at": 0,
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "input_tokens_details": {"cached_tokens": 10},
                            "output_tokens_details": {"reasoning_tokens": 5},
                        },
                    }
                )
                self.items.extend(
                    [
                        {
                            "id": root + ":input",
                            "turn_id": root,
                            "type": "message",
                            "role": "user",
                            "status": "completed",
                            "content": [{"type": "input_text", "text": text}],
                        },
                        {
                            "id": root + ":answer",
                            "turn_id": root,
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": "answer " + root}],
                        },
                    ]
                )
            self.sent.set()
            return httpx.Response(202, json={})
        if path.endswith("/turns") or path.endswith("/items"):
            values = self.turns if path.endswith("/turns") else self.items
            return httpx.Response(
                200,
                json={
                    "data": values,
                    "has_more": False,
                    "last_id": values[-1]["id"] if values else None,
                },
            )
        if "/turns/" in path:
            return httpx.Response(
                200,
                json={**self.turns[-1], "status": "cancelled" if self.cancelled else "completed"},
            )
        return httpx.Response(200, json=self.session())


@pytest.fixture(params=["memory", "postgres"])
async def composed(request, db_session_factory, db_clean, monkeypatch):
    wire = Wire()
    from daimon.core.stores.mux_state import PostgresStateStore
    from daimon.testing.factories import make_tenant

    async with db_session_factory() as db, db.begin():
        await make_tenant(db, id=TENANT)
    store = (
        MemoryStateStore() if request.param == "memory" else PostgresStateStore(db_session_factory)
    )
    observations = []

    async def record(**kwargs):
        observations.append(kwargs)
        return False  # durable pending, never a known free turn

    monkeypatch.setattr(openai_host, "record_provider_usage", record)

    async def plan(request):
        return SessionSpec(
            agent=ResourceRef(
                id="native-agent",
                kind="agent",
                provider="openai",
                account_scope_id="project",
                tenant_id=SCOPE.tenant_id,
                account_id=SCOPE.account_id,
            ),
            agent_revision=Revision(local=0),
            config_revision=REVISION.local,
        )

    async with AsyncOpenAI(
        api_key="offline",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
    ) as sdk:
        runtime = OpenAIHostRuntime(
            lambda revision, scope: SDKTransport(sdk),
            OpenAIRecoveryJournal(),
            OpenAIUsageRevisions(),
            account_scope_id="project",
            session_plan=plan,
            authorization=lambda scope, kind, id_: scope == SCOPE,
            controls=SessionControls(
                model="gpt-6-luna",
                multi_agent_enabled=False,
                container_size="small",
                spend_limit_usd_cents=4,
            ),
        )
        deps = replace(
            deps_for(None, db_session_factory),
            channel_backends=True,
            state_store=store,
            turn_runtimes={PROFILE: runtime},
        )
        request = ProviderPreparationRequest(
            deps,
            admission(),
            SCOPE,
            TENANT,
            "slack",
            "caller",
            "thread",
            ACCOUNT,
            True,
            cast(object, None),
            None,
            datetime.now(UTC) + timedelta(seconds=20),
            lambda: datetime.now(UTC),
        )
        yield request, wire, store, observations, runtime


def test_explicit_luna_registration_and_unchanged_default():
    assert channel_backend.check_backend(REVISION).profile_id == PROFILE
    from mux.contracts.config import BackendConfig, resolve_default

    assert resolve_default(BackendConfig()).profile == "anthropic.managed_agents"
    with pytest.raises(channel_backend.BackendUnsupported):
        channel_backend.check_backend(REVISION.model_copy(update={"model": "gpt-6-astra"}))


async def test_native_preparation_binding_and_reuse(composed):
    request, wire, store, _, _ = composed
    first = await prepare_openai(request)
    second = await prepare_openai(request)
    assert first.session_ref == second.session_ref
    assert first.mapping_id is None and not first.reused and second.reused
    binding = await store.get_binding(
        Slot(
            thread=ThreadRef(channel=REVISION.channel, thread_id="thread"),
            account_id=SCOPE.account_id,
        )
    )
    assert binding.native_refs["session"] == SESSION_ID
    assert binding.native_refs["agent"] == "native-agent"
    assert binding.native_refs["model"] == "gpt-6-luna"
    assert len([r for r in wire.requests if r.method == "POST"]) == 1


async def test_creation_unknown_is_never_resent(composed):
    request, wire, _, _, _ = composed
    wire.fail_create = True
    from mux.errors import ProviderError

    with pytest.raises(ProviderError):
        await prepare_openai(request)
    wire.fail_create = False
    with pytest.raises(UncertainSend):
        await prepare_openai(request)
    assert len([r for r in wire.requests if r.method == "POST"]) == 1


async def test_wrong_native_model_cannot_run(composed):
    request, wire, _, _, _ = composed
    wire.bad_model = True
    with pytest.raises(ContinuityLost):
        await prepare_openai(request)


async def test_model_refusal_before_transport_or_plan(composed):
    request, wire, _, _, runtime = composed
    with pytest.raises(UnsupportedCapability):
        runtime_for(runtime, REVISION.model_copy(update={"model": "gpt-6-astra"}))
    assert wire.requests == []


async def test_two_host_turns_persist_usage_and_root_isolation(composed):
    request, wire, store, observations, runtime = composed
    from daimon.core.turn.outcomes import drain_outcomes
    from daimon.core.turn.prepare import bind_session_impl
    from daimon.core.turn.run import run_prepared_turn

    for index in (1, 2):
        prepared = await bind_session_impl(
            request.deps,
            request.admission,
            tenant_id=TENANT,
            platform="slack",
            external_user_id="caller",
            thread_id="thread",
            session_account_id=ACCOUNT,
            reuse_existing=True,
        )

        async def reseed():
            return "question"

        outcome = await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=TENANT,
            platform="slack",
            thread_id="thread",
            external_user_id="caller",
            user_message=f"question {index}",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            render_interval_s=0.01,
            operation_key=f"host-{index}",
        )
        result = outcome.state
        await drain_outcomes()
        assert result.error is None
        assert result.usage_totals is not None
        assert f"answer turn-{index}" in str(result.content)
        if index == 2:
            assert "answer turn-1" not in str(result.content)
    assert {x["observation"].turn_id for x in observations} == {"turn-1", "turn-2"}
    assert all(x["infrastructure_usd"] is None for x in observations)
    assert all(x["model_id"] == "gpt-6-luna" for x in observations)
    rows = await store.read_events(SESSION_ID)
    assert sum(e.type == "session.turn_ended" for e in rows) == 2
    assert sum(r.method == "POST" and r.url.path.endswith("/events") for r in wire.requests) == 2
    assert all(s.closed for s in wire.sources)


@pytest.mark.parametrize("composed", ["postgres"], indirect=True)
async def test_actual_postgres_pending_then_measured_settlement(composed, monkeypatch):
    from datetime import date
    from decimal import Decimal

    from daimon.core.pricing import ProviderPrice
    from daimon.core.stores.tenant_ledger import get_balance
    from daimon.core.usage_recording import record_provider_usage

    monkeypatch.setattr(openai_host, "record_provider_usage", record_provider_usage)
    request, wire, store, _, runtime = composed
    first = await prepare_openai(request)
    binding = await store.get_binding(
        Slot(
            thread=ThreadRef(channel=REVISION.channel, thread_id="thread"),
            account_id=SCOPE.account_id,
        )
    )
    # Meter returned by the fake provider, not an Anthropic model span.
    wire.turns = [
        {
            "id": "root",
            "session_id": SESSION_ID,
            "agent_id": "native-agent",
            "subagent_id": None,
            "status": "completed",
            "created_at": 0,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 10},
            },
        }
    ]
    active = TurnPersistence(store, binding, SCOPE, operation_key="postrun")

    async def read_usage():
        page = await first.backend.usage.list(SCOPE, first.session_ref, page=PageRequest())
        return page.data[0]

    with active.activate():
        usage = await active.run(read_usage)
    recorder = cast(ObservationRecorder, first._record)
    assert await recorder(observation=usage) is False
    assert len(await store.pending_outbox()) == 1

    async def measured(observation):
        return Decimal("0.0053")

    measured_runtime = replace(
        runtime,
        price=ProviderPrice(
            provider="openai",
            model="gpt-6-luna",
            checked_on=date(2026, 10, 10),
            input=Decimal("0.10"),
            output=Decimal("0.50"),
            cache_read=Decimal("0.01"),
        ),
        infrastructure=measured,
    )
    next_request = replace(
        request, deps=replace(request.deps, turn_runtimes={PROFILE: measured_runtime})
    )
    second = await prepare_openai(next_request)
    settled = cast(ObservationRecorder, second._record)
    assert await settled(observation=usage) is True
    async with request.deps.sessionmaker() as db:
        before = await get_balance(db, tenant_id=TENANT)
    assert await settled(observation=usage) is False
    async with request.deps.sessionmaker() as db:
        assert await get_balance(db, tenant_id=TENANT) == before
    assert before == Decimal("-0.005319")
    assert await store.pending_outbox() == []


async def test_foreign_plan_and_changed_plan_refuse_before_new_session(composed):
    request, wire, store, _, runtime = composed
    await prepare_openai(request)

    async def changed(req):
        spec = await runtime.session_plan(req)
        return spec.model_copy(update={"metadata": {"changed": "yes"}})

    next_request = replace(
        request,
        deps=replace(request.deps, turn_runtimes={PROFILE: replace(runtime, session_plan=changed)}),
    )
    count = len(wire.requests)
    with pytest.raises(ContinuityLost):
        await prepare_openai(next_request)
    assert len(wire.requests) == count

    async def foreign(req):
        spec = await runtime.session_plan(req)
        return spec.model_copy(
            update={"agent": spec.agent.model_copy(update={"account_id": "foreign"})}
        )

    next_request = replace(
        request,
        deps=replace(request.deps, turn_runtimes={PROFILE: replace(runtime, session_plan=foreign)}),
    )
    with pytest.raises(ScopeViolation):
        await prepare_openai(next_request)
    assert len(wire.requests) == count


async def test_restart_replays_same_claim_without_second_post(composed):
    from daimon.core.mux_backend import TurnBackendRequest
    from daimon.core.turn.io import turn_io

    request, wire, store, _, runtime = composed
    prepared = await prepare_openai(request)
    binding = await store.get_binding(
        Slot(
            thread=ThreadRef(channel=REVISION.channel, thread_id="thread"),
            account_id=SCOPE.account_id,
        )
    )

    def context():
        return TurnPersistence(store, binding, SCOPE, operation_key="restart-invocation")

    def codec(active):
        return turn_io(
            request.deps.anthropic,
            SESSION_ID,
            path="mux",
            scope=SCOPE,
            profile=PROFILE,
            backend_request=TurnBackendRequest(
                PROFILE,
                request.deps.anthropic,
                SCOPE,
                SESSION_ID,
                config=REVISION,
                session=prepared.session_ref,
                runtime=runtime,
            ),
            persistence=active,
        )

    active = context()

    async def send_initial():
        io = codec(active)
        await io.send([{"type": "user.message", "content": [{"type": "text", "text": "question"}]}])

    with active.activate():
        await active.run(send_initial)
    posts = len([r for r in wire.requests if r.method == "POST" and r.url.path.endswith("/events")])
    active = context()

    async def restore():
        io = codec(active)
        # Same request restores an accepted receipt; there is no new POST.
        await io.send([{"type": "user.message", "content": [{"type": "text", "text": "question"}]}])
        values = await io.replay(timeout_s=2)
        usage = await io.replay_usage()
        return values, usage

    with active.activate():
        values, usage = await active.run(restore)
    assert any(e.type == "agent.message" for e in values)
    assert values[-1].type == "session.status_idle"
    assert [u.turn_id for u in usage] == ["turn-1"]
    assert (
        len([r for r in wire.requests if r.method == "POST" and r.url.path.endswith("/events")])
        == posts
        == 1
    )
    assert all(source.closed for source in wire.sources)


async def test_actual_cancel_ack_waits_and_completes_claim(composed):
    from daimon.core.mux_backend import TurnBackendRequest
    from daimon.core.turn.io import turn_io

    request, wire, store, _, runtime = composed
    prepared = await prepare_openai(request)
    binding = await store.get_binding(
        Slot(
            thread=ThreadRef(channel=REVISION.channel, thread_id="thread"),
            account_id=SCOPE.account_id,
        )
    )
    active = TurnPersistence(store, binding, SCOPE, operation_key="cancel-invocation")

    async def work():
        io = turn_io(
            request.deps.anthropic,
            SESSION_ID,
            path="mux",
            scope=SCOPE,
            profile=PROFILE,
            backend_request=TurnBackendRequest(
                PROFILE,
                request.deps.anthropic,
                SCOPE,
                SESSION_ID,
                config=REVISION,
                session=prepared.session_ref,
                runtime=runtime,
            ),
            persistence=active,
        )
        await io.send([{"type": "user.message", "content": [{"type": "text", "text": "question"}]}])
        # Exercise native running-root lookup without consuming a fabricated id.
        wire.turns[-1]["status"] = "in_progress"
        stopped = await io.interrupt(timeout_s=2)
        assert stopped.stopped and stopped.outcome == "interrupted"
        receipt = await store.get_operation(SCOPE, "cancel-invocation:cancel:0")
        assert receipt.operation.status == "processed"

    # The session must actually report a running root for cancellation discovery.
    original = wire.session

    def running():
        value = original()
        if wire.turns and not wire.cancelled:
            value["status"] = "in_progress"
        return value

    wire.session = running
    with active.activate():
        await active.run(work)
    rows = await store.read_events(SESSION_ID)
    assert any(
        e.type == "session.turn_ended" and e.payload["outcome"] == "interrupted" for e in rows
    )
    assert (
        len(
            [
                r
                for r in wire.requests
                if r.method == "POST" and r.headers.get("Idempotency-Key", "").endswith(":cancel:0")
            ]
        )
        == 1
    )


async def test_durable_usage_revision_survives_runtime_restart(composed):
    request, _, store, _, _ = composed
    prepared = await prepare_openai(request)
    binding = await store.get_binding(
        Slot(
            thread=ThreadRef(channel=REVISION.channel, thread_id="thread"),
            account_id=SCOPE.account_id,
        )
    )

    async def allocate(meter):
        active = TurnPersistence(store, binding, SCOPE, operation_key="revision")

        async def call():
            return await OpenAIUsageRevisions().revise(
                prepared.session_ref, "openai:turn:root", meter
            )

        with active.activate():
            return await active.run(call)

    assert await allocate({"usage": None, "subagent_id": None}) == 1
    assert await allocate({"usage": None, "subagent_id": None}) == 1
    assert await allocate({"usage": {"input_tokens": 3}, "subagent_id": None}) == 2
    assert await allocate({"usage": {"input_tokens": 3}, "subagent_id": None}) == 2
    with pytest.raises(ScopeViolation):
        await OpenAIUsageRevisions().revise(prepared.session_ref, "root", {})


@pytest.mark.parametrize("composed", ["postgres"], indirect=True)
async def test_origin_approval_has_separate_claim_and_native_response(composed):
    from daimon.core.mux_backend import TurnBackendRequest
    from daimon.core.turn.io import turn_io

    request, wire, store, _, runtime = composed
    prepared = await prepare_openai(request)
    binding = await store.get_binding(
        Slot(
            thread=ThreadRef(channel=REVISION.channel, thread_id="thread"),
            account_id=SCOPE.account_id,
        )
    )
    active = TurnPersistence(store, binding, SCOPE, operation_key="approval-invocation")
    original = wire.handle
    posts = []

    def with_origin(req):
        if req.method == "GET" and req.url.path.endswith("/" + SESSION_ID) and wire.turns:
            value = wire.session()
            value["status"] = "requires_action"
            value["required_actions"] = [
                {
                    "type": "computer_use_approval_request",
                    "request_id": "origin-1",
                    "turn_id": "turn-1",
                    "request": {
                        "type": "browser_origin_access",
                        "origin": "https://example.com",
                        "reason": "requested page",
                    },
                }
            ]
            return httpx.Response(200, json=value)
        if req.method == "POST" and req.url.path.endswith("/events"):
            body = json.loads(req.content)
            if (
                body["events"][0]["type"]
                == "agent.session.input.computer_use_approval_request_result"
            ):
                posts.append((req.headers["Idempotency-Key"], body))
                return httpx.Response(202, json={})
        return original(req)

    # A new private transport binds the actual SDK fake to this documented shape.
    async with AsyncOpenAI(
        api_key="offline",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(with_origin)),
    ) as sdk:
        rt = replace(runtime, transport_factory=lambda revision, scope: SDKTransport(sdk))

        async def work():
            io = turn_io(
                request.deps.anthropic,
                SESSION_ID,
                path="mux",
                scope=SCOPE,
                profile=PROFILE,
                backend_request=TurnBackendRequest(
                    PROFILE,
                    request.deps.anthropic,
                    SCOPE,
                    SESSION_ID,
                    config=REVISION,
                    session=prepared.session_ref,
                    runtime=rt,
                ),
                persistence=active,
            )
            await io.send(
                [{"type": "user.message", "content": [{"type": "text", "text": "question"}]}]
            )
            wire.turns[-1]["status"] = "waiting"
            await io.send(
                [
                    {
                        "type": "user.tool_confirmation",
                        "tool_use_id": "openai:tool:origin-1",
                        "result": "allow",
                    }
                ]
            )
            with pytest.raises(OperationConflict):
                await io.send(
                    [
                        {
                            "type": "user.tool_confirmation",
                            "tool_use_id": "openai:tool:origin-1",
                            "result": "deny",
                        }
                    ]
                )

        with active.activate():
            await active.run(work)
    assert len(posts) == 1
    assert ":origin:" in posts[0][0] and ":send:" not in posts[0][0]
    assert posts[0][1] == {
        "events": [
            {
                "type": "agent.session.input.computer_use_approval_request_result",
                "request_id": "origin-1",
                "response": {"type": "browser_origin_access", "decision": "approve"},
            }
        ]
    }


@pytest.mark.parametrize("composed", ["postgres"], indirect=True)
@pytest.mark.parametrize("policy", ["memory_read_only", "source_sealed", "asks_before_publishing"])
async def test_unimplemented_restricted_policy_refuses_before_transport(composed, policy):
    from daimon.core.turn.errors import AdmissionDenied

    request, wire, _, _, _ = composed
    request = replace(request, admission=replace(request.admission, **{policy: True}))
    with pytest.raises(AdmissionDenied) as refused:
        await prepare_openai(request)
    assert refused.value.reason == "backend_unsupported"
    assert wire.requests == []


@pytest.mark.parametrize("missing_runtime", [True, False])
async def test_configured_channel_refuses_unavailable_runtime_before_io(composed, missing_runtime):
    from daimon.core.turn.errors import AdmissionDenied
    from daimon.core.turn.prepare import bind_session_impl

    request, wire, _, _, runtime = composed

    def forbidden_transport(revision, scope):
        raise AssertionError("a refused channel must not access a key or transport")

    async def forbidden_plan(request):
        raise AssertionError("a refused channel must not resolve native resources")

    runtime = replace(runtime, transport_factory=forbidden_transport, session_plan=forbidden_plan)
    deps = replace(request.deps, turn_runtimes={} if missing_runtime else {PROFILE: runtime})
    admission = (
        request.admission
        if missing_runtime
        else replace(request.admission, asks_before_publishing=True)
    )
    with pytest.raises(AdmissionDenied) as refused:
        await bind_session_impl(
            deps,
            admission,
            tenant_id=TENANT,
            platform="slack",
            external_user_id="caller",
            thread_id="thread",
            session_account_id=ACCOUNT,
            reuse_existing=True,
        )
    assert refused.value.reason == "backend_unsupported"
    assert wire.requests == []


async def test_delegation_control_is_required_before_native_io(composed):
    from daimon.core.turn.errors import AdmissionDenied

    request, wire, _, _, runtime = composed
    runtime = replace(
        runtime, controls=runtime.controls.model_copy(update={"multi_agent_enabled": None})
    )
    request = replace(request, deps=replace(request.deps, turn_runtimes={PROFILE: runtime}))
    with pytest.raises(AdmissionDenied) as refused:
        await prepare_openai(request)
    assert refused.value.reason == "backend_unsupported"
    assert wire.requests == []


async def test_saved_delegated_agent_refuses_before_session_post(composed):
    from daimon.core.turn.errors import AdmissionDenied

    request, wire, _, _, _ = composed
    wire.saved_delegation = True
    with pytest.raises(AdmissionDenied) as refused:
        await prepare_openai(request)
    assert refused.value.reason == "backend_unsupported"
    assert all(request.method == "GET" for request in wire.requests)


async def test_reused_delegated_session_cannot_settle_only_its_root(composed):
    from daimon.core.turn.errors import AdmissionDenied

    request, wire, store, observations, _ = composed
    await prepare_openai(request)
    wire.session_delegation = True
    # Both model meters would be billable. This bounded host refuses the
    # delegated session before input/usage callbacks, rather than debit root
    # 100 and silently omit the child's million tokens.
    wire.turns = [
        {
            "id": "root",
            "session_id": SESSION_ID,
            "subagent_id": None,
            "agent_id": "native-agent",
            "status": "completed",
            "created_at": 0,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
        {
            "id": "child",
            "session_id": SESSION_ID,
            "subagent_id": "delegate",
            "agent_id": "native-agent",
            "status": "completed",
            "created_at": 0,
            "usage": {"input_tokens": 1000000, "output_tokens": 20},
        },
    ]
    with pytest.raises(AdmissionDenied) as refused:
        await prepare_openai(request)
    assert refused.value.reason == "backend_unsupported"
    assert observations == []
    assert await store.pending_outbox() == []
    assert not any(r.method == "POST" and r.url.path.endswith("/events") for r in wire.requests)


async def test_delegation_drift_after_preparation_refuses_before_input(composed):
    from daimon.core.turn.outcomes import drain_outcomes
    from daimon.core.turn.run import run_prepared_turn
    from daimon.core.turn.termination import TerminationReason

    request, wire, _, _, _ = composed
    prepared = await prepare_openai(request)
    wire.session_delegation = True

    async def reseed():
        raise AssertionError("delegation refusal must never retry input")

    lifecycle = RecordingLifecycle()
    outcome = await run_prepared_turn(
        request.deps,
        prepared,
        tenant_id=TENANT,
        platform="slack",
        thread_id="thread",
        external_user_id="caller",
        user_message="question",
        lifecycle=lifecycle,
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        operation_key="delegation-drift",
    )
    await drain_outcomes()
    assert outcome.state.termination == TerminationReason.UPSTREAM
    assert outcome.state.error is not None
    assert lifecycle.terminal_success == []
    assert not any(r.method == "POST" and r.url.path.endswith("/events") for r in wire.requests)
    assert all(source.closed for source in wire.sources)


@pytest.mark.parametrize("composed", ["postgres"], indirect=True)
async def test_reauthorization_change_before_create_cannot_post(composed, monkeypatch):
    request, wire, _, _, _ = composed
    calls = 0

    async def reauthorize(deps, admitted):
        nonlocal calls
        calls += 1
        return admitted if calls == 1 else replace(admitted, source_sealed=True)

    monkeypatch.setattr(openai_host, "reauthorize", reauthorize)
    from daimon.core.turn.errors import AdmissionDenied

    with pytest.raises(AdmissionDenied):
        await prepare_openai(request)
    assert wire.requests == []


@pytest.mark.parametrize("mode", ["live", "replay"])
@pytest.mark.parametrize("native_status", ["failed", "cancelled"])
async def test_final_host_outcome_preserves_observed_root(
    composed, monkeypatch, mode, native_status
):
    """Native terminal -> fenced journal -> actual final host/lifecycle result."""
    from daimon.core.turn.outcomes import drain_outcomes
    from daimon.core.turn.prepare import bind_session_impl
    from daimon.core.turn.run import run_prepared_turn
    from daimon.core.turn.termination import TerminationReason

    request, wire, store, _, _ = composed

    class OutcomeStream(Stream):
        async def __aiter__(self):
            # The recovery surveillance stream remains open until history is
            # atomically published; an eventless loop would not prove replay.
            if mode == "replay" and len(self.wire.sources) % 2 == 0:
                await asyncio.Event().wait()
            if mode == "replay" and len(self.wire.sources) > 1:
                return
            await self.wire.sent.wait()
            root = self.wire.turns[-1]
            root["status"] = native_status
            events = [
                {
                    "type": "agent.session.turn.in_progress",
                    "event_id": root["id"] + ":running",
                    "turn": {**root, "status": "in_progress"},
                },
                {
                    "type": "agent.session.turn.item.done",
                    "event_id": root["id"] + ":answer",
                    "turn_id": root["id"],
                    "item": self.wire.items[-1],
                },
            ]
            if mode == "live":
                events.append(
                    {
                        "type": "agent.session.turn." + native_status,
                        "event_id": root["id"] + ":done",
                        "turn": dict(root),
                    }
                )
            for event in events:
                event["session_id"] = SESSION_ID
                yield ("data: " + json.dumps(event) + "\n\n").encode()
            if mode == "replay":
                raise httpx.ReadError("offline stream lost before terminal")
            await asyncio.Event().wait()

    monkeypatch.setattr(sys.modules[__name__], "Stream", OutcomeStream)
    prepared = await bind_session_impl(
        request.deps,
        request.admission,
        tenant_id=TENANT,
        platform="slack",
        external_user_id="caller",
        thread_id="thread",
        session_account_id=ACCOUNT,
        reuse_existing=True,
    )

    async def reseed():
        raise AssertionError("a failed or cancelled root must not be resent")

    lifecycle = RecordingLifecycle()
    outcome = await run_prepared_turn(
        request.deps,
        prepared,
        tenant_id=TENANT,
        platform="slack",
        thread_id="thread",
        external_user_id="caller",
        user_message="question",
        lifecycle=lifecycle,
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        render_interval_s=0.01,
        operation_key="native-outcome-" + mode + "-" + native_status,
    )
    await drain_outcomes()
    terminals = [r for r in await store.read_events(SESSION_ID) if r.type == "session.turn_ended"]
    assert terminals and terminals[-1].payload["outcome"] == (
        "errored" if native_status == "failed" else "interrupted"
    )
    assert sum(r.method == "POST" and r.url.path.endswith("/events") for r in wire.requests) == 1
    assert all(source.closed for source in wire.sources)
    assert outcome.state.termination == (
        TerminationReason.UPSTREAM if native_status == "failed" else TerminationReason.INTERRUPTED
    )
    assert (
        lifecycle.terminal_success == []
        if native_status == "failed" or mode == "replay"
        else len(lifecycle.terminal_success) == 1
    )
    if native_status == "failed" or mode == "replay":
        assert outcome.state.error is not None
        assert len(lifecycle.terminal_failures) == 1
