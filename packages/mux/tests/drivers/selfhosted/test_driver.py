"""The real driver against an in-process C3 HTTP fake, without a model SDK."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from mux.contracts.actions import UserMessage, UserToolConfirmation
from mux.contracts.events import TextPart
from mux.contracts.ids import ModelRef, PageRequest, Revision, Scope
from mux.contracts.ports import Agents, Environments
from mux.contracts.resources import Agent, AgentSpec, Environment, EnvironmentSpec, SessionSpec
from mux.drivers.selfhosted import SelfHostedDriver
from mux.drivers.selfhosted.transport import (
    DirectTransport,
    FoundryInvocationsTransport,
    sandbox_id,
)
from mux.errors import OperationConflict, ProviderError, ScopeViolation

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="person", authorization_id="grant"
)


class Harness:
    def __init__(self, mode="complete", foundry=False):
        self.mode, self.foundry = mode, foundry
        self.events = []
        self.requests = {}
        self.executions = 0
        self.calls = []
        self.request = None

    def emit(self, type_, data):
        if type_ in ("agent_message", "agent_message_delta"):
            data = {"message_id": self.request["turn_id"] + ":message", **data}
        self.events.append(
            dict(
                seq=len(self.events) + 1,
                session_id=self.request["session_id"],
                turn_id=self.request["turn_id"],
                at=datetime.now(UTC).isoformat(),
                type=type_,
                data=data,
            )
        )

    def finish(self, outcome="completed"):
        self.emit("agent_message", {"parts": [{"type": "text", "text": "done"}]})
        self.emit("turn_ended", {"outcome": outcome})

    def handle(self, request):
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.headers["accept"] == "application/json"
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        query = dict(request.url.params)
        if self.foundry:
            assert method == "POST"
            assert path == "/project/agents/agent/endpoint/protocols/invocations"
            assert request.url.params["api-version"] == "v1"
            sandbox = request.url.params["agent_session_id"]
            envelope = body["c3"]
            method, path, body = envelope["method"], envelope["path"], envelope["body"]
            query = envelope["query"]
            self.calls.append(sandbox)
        if path.endswith("/events"):
            session_id = path.split("/")[-2]
            values = [event for event in self.events if event["session_id"] == session_id]
            return httpx.Response(
                200,
                json={
                    "events": [
                        event for event in values if event["seq"] > int(query.get("after_seq", 0))
                    ],
                    "complete": not values or values[-1]["type"] == "turn_ended",
                },
            )
        if path == "/v0/turns":
            self.request = body
            key = body["idempotency_key"]
            if key not in self.requests:
                self.requests[key] = body
                self.executions += 1
                self.emit("status_running", {})
                if self.mode not in ("unmetered", "pre_model"):
                    self.emit(
                        "usage",
                        dict(
                            request_id="request-1",
                            model=body["model"],
                            input_tokens=10,
                            cached_input_tokens=2,
                            output_tokens=5,
                            reasoning_tokens=1,
                        ),
                    )
                if self.mode == "approval":
                    self.emit(
                        "requires_action",
                        dict(
                            action_id="approval",
                            kind="tool_confirmation",
                            call_id="call",
                            name="bash",
                            input={"command": "echo hi"},
                        ),
                    )
                elif self.mode not in ("running", "pre_model"):
                    self.emit("agent_message_delta", {"text": "do"})
                    self.emit("agent_message_delta", {"text": "ne"})
                    self.emit(
                        "tool_use",
                        dict(
                            call_id="call",
                            name="bash",
                            input={"command": "echo hi"},
                            executor="agent",
                        ),
                    )
                    self.emit("tool_result", dict(call_id="call", output="hi", is_error=False))
                    self.finish()
            else:
                assert self.requests[key] == body
            return httpx.Response(202, json={"accepted": True, "turn_id": body["turn_id"]})
        if path.endswith("/actions"):
            assert body["action_id"] == "approval"
            self.emit("status_running", {})
            self.finish()
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/cancel"):
            self.finish("interrupted")
            return httpx.Response(200, json={"cancelled": True})
        raise AssertionError(path)


class Resources:
    def __init__(self, ref, cls, spec):
        self.value = cls(
            ref=ref, revision=Revision(local=1), spec=spec, created_at=datetime.now(UTC)
        )

    async def retrieve(self, scope, ref):
        assert scope == SCOPE
        assert ref == self.value.ref
        return self.value


async def setup(client, harness, **options):
    from mux.contracts.ids import ResourceRef

    def ref(kind):
        return ResourceRef(
            id=kind,
            kind=kind,
            provider="selfhosted",
            account_scope_id="tenant",
            tenant_id=SCOPE.tenant_id,
            account_id=None,
        )

    agents = cast(
        Agents,
        Resources(
            ref("agent"),
            Agent,
            AgentSpec(
                name="test",
                model=ModelRef(provider="selfhosted", id="gpt-6-luna"),
                system="test instructions",
            ),
        ),
    )
    envs = cast(
        Environments,
        Resources(
            ref("environment"), Environment, EnvironmentSpec(name="test", execution="self_hosted")
        ),
    )
    transport = (
        FoundryInvocationsTransport(
            client,
            project_endpoint="https://harness/project",
            agent_name="agent",
            token="test-token",
        )
        if harness.foundry
        else DirectTransport(client, base_url="https://harness", token="test-token")
    )
    options.setdefault("deployment", "gpt-6-luna")
    driver = SelfHostedDriver(
        transport,
        account_scope_id="tenant",
        agents=agents,
        environments=envs,
        authorization=lambda scope, kind, id_: scope == SCOPE,
        **options,
    )
    session = await driver.sessions.create(
        SCOPE,
        SessionSpec(
            agent=ref("agent"),
            agent_revision=Revision(local=1),
            environment=ref("environment"),
            config_revision=1,
        ),
        key="create",
    )
    return driver, session


MESSAGE = UserMessage(content=(TextPart(text="hello"),))


@pytest.mark.parametrize("foundry", [False, True])
async def test_stream_tools_usage_and_idempotent_resend(foundry):
    fake = Harness(foundry=foundry)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        receipt = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        assert await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send") == receipt
        events = [
            e async for e in await driver.events.open_stream(SCOPE, session.ref, previews=True)
        ]
        assert [e.type for e in events] == [
            "session.status_running",
            "usage.observed",
            "agent.message.delta",
            "agent.message.delta",
            "agent.tool_use",
            "agent.tool_result",
            "agent.message",
            "session.turn_ended",
        ]
        assert fake.executions == 1
        usage = await driver.usage.reconcile(SCOPE, session.ref)
        assert usage[0].input_tokens == 10
        assert usage[0].input_cached_tokens == 2
        assert usage[0].output_reasoning_tokens == 1
        assert usage[0].completeness == "measured"
        assert await driver.usage.reconcile(SCOPE, session.ref) == usage
        with pytest.raises(OperationConflict):
            await driver.events.send(
                SCOPE, session.ref, [UserMessage(content=(TextPart(text="different"),))], key="send"
            )
        if foundry:
            assert set(fake.calls) == {sandbox_id(session.ref.id)}
            assert session.binding.native_refs["sandbox"] == sandbox_id(session.ref.id)


async def test_approval_and_reconcile_after_lost_reader():
    fake = Harness("approval")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        receipt = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        source = await driver.events.open_stream(SCOPE, session.ref, after="0")
        first = await anext(source)
        await source.aclose()
        snapshot = await driver.events.reconcile(SCOPE, session.ref)
        assert snapshot.state == "requires_action"
        assert snapshot.required_actions[0].id == "approval"
        await driver.events.send(
            SCOPE,
            session.ref,
            [UserToolConfirmation(action_id="approval", decision="allow")],
            key="approve",
            expected_turn=receipt.turn_id,
        )
        remaining = [
            e
            async for e in await driver.events.open_stream(
                SCOPE, session.ref, after=str(first.sequence)
            )
        ]
        assert remaining[-1].type == "session.turn_ended"
        assert (await driver.events.reconcile(SCOPE, session.ref)).state == "idle"
        page = await driver.events.list(SCOPE, session.ref, page=PageRequest(limit=2))
        assert page.has_more
        second = await driver.events.list(
            SCOPE, session.ref, page=PageRequest(cursor=page.next_cursor)
        )
        assert all(e.sequence > page.data[-1].sequence for e in second.data)


async def test_cancel_wait_and_foreign_scope_refusal():
    fake = Harness("running")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        sent = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        cancelled = await driver.events.cancel(
            SCOPE, session.ref, turn_id=sent.turn_id, key="cancel"
        )
        assert cancelled.status == "requested"
        stopped = await driver.events.wait_stopped(
            SCOPE, cancelled, deadline=datetime.now(UTC) + timedelta(seconds=1)
        )
        assert stopped.stopped and stopped.outcome == "interrupted"
        assert (
            await driver.events.cancel(SCOPE, session.ref, turn_id=sent.turn_id, key="again")
        ).status == "already_stopped"
        foreign = SCOPE.model_copy(update={"tenant_id": "other"})
        with pytest.raises(ScopeViolation):
            await driver.events.reconcile(foreign, session.ref)


async def test_unmetered_completion_is_retained_unresolved_and_next_turn_works():
    fake = Harness("unmetered")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        receipt = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="first")
        events = [
            event async for event in await driver.events.open_stream(SCOPE, session.ref, after="0")
        ]
        assert events[-1].payload["outcome"] == "errored"
        assert events[-1].payload["native_reason"] == "turn_usage_required"
        assert driver.events.unresolved_turns[session.ref] == {receipt.turn_id}
        assert (await driver.events.reconcile(SCOPE, session.ref)).state == "idle"
        fake.mode = "complete"
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="second")
        assert (await driver.events.reconcile(SCOPE, session.ref)).state == "idle"
        assert fake.executions == 2


async def test_restart_resend_replays_one_turn_and_usage_identity():
    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        first = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        usage = await driver.usage.reconcile(SCOPE, session.ref)
        reconstructed, _ = await setup(client, fake)
        repeated = await reconstructed.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        assert first.turn_id == repeated.turn_id
        assert fake.executions == 1
        assert await reconstructed.usage.reconcile(SCOPE, session.ref) == usage


async def test_usage_unavailable_stays_unknown_and_preserves_request_id():
    fake = Harness("running")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        fake.events = fake.events[:1]
        fake.emit(
            "error",
            dict(
                kind="usage_unavailable",
                message="request cancelled",
                retryable=False,
                request_id="failed-request",
            ),
        )
        fake.finish("interrupted")
        observations = await driver.usage.reconcile(SCOPE, session.ref)
        assert len(observations) == 1
        assert observations[0].completeness == "unknown"
        assert observations[0].input_tokens is None
        assert observations[0].output_tokens is None
        assert observations[0].id.endswith(":request:failed-request")


async def test_models_only_admit_configured_deployment():
    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, _ = await setup(client, fake)
        assert (
            await driver.models.validate(SCOPE, ModelRef(provider="selfhosted", id="gpt-6-luna"))
        ).admitted
        assert not (
            await driver.models.validate(SCOPE, ModelRef(provider="openai", id="gpt-6-luna"))
        ).admitted
        assert not (
            await driver.models.validate(SCOPE, ModelRef(provider="selfhosted", id="unknown"))
        ).admitted


async def test_unmatched_request_start_remains_pending_then_measured_revision_supersedes():
    fake = Harness("running")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        fake.events = fake.events[:1]
        fake.emit("model_request_started", {"request_id": "request-1", "model": "gpt-6-luna"})
        pending = await driver.usage.reconcile(SCOPE, session.ref)
        assert len(pending) == 1
        assert pending[0].completeness == "unknown"
        assert pending[0].input_tokens is None and pending[0].output_tokens is None
        fake.emit(
            "usage",
            dict(
                request_id="request-1",
                model="gpt-6-luna",
                input_tokens=12,
                cached_input_tokens=0,
                output_tokens=3,
                reasoning_tokens=0,
            ),
        )
        fake.finish()
        measured = await driver.usage.reconcile(SCOPE, session.ref)
        assert len(measured) == 1
        assert measured[0].id == pending[0].id
        assert measured[0].revision > pending[0].revision
        assert measured[0].completeness == "measured"
        assert measured[0].input_tokens == 12
        assert measured[0].output_tokens == 3


async def test_terminal_unmatched_request_start_is_unknown_without_invented_tokens():
    fake = Harness("running")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        fake.events = fake.events[:1]
        fake.emit("model_request_started", {"request_id": "crash-window", "model": "gpt-6-luna"})
        fake.finish("interrupted")
        assert (await driver.events.reconcile(SCOPE, session.ref)).state == "idle"
        pending = await driver.usage.reconcile(SCOPE, session.ref)
        assert pending[0].completeness == "unknown"
        assert pending[0].id.endswith(":request:crash-window")
        assert pending[0].input_tokens is None


@pytest.mark.parametrize("fixture_id", ["C06", "C10", "C15", "C16"])
async def test_real_driver_passes_existing_mux_conformance_probes(fixture_id):
    from daimon.testing.conformance.runner import Adapter, Scenario, ScriptedTransport, run_fixture

    class ConformanceHarness(Harness):
        mutation_count = 0

        def handle(self, request):
            if request.url.path.endswith("/cancel"):
                self.mutation_count += 1
                return httpx.Response(200, json={"cancelled": True})
            return super().handle(request)

        async def arrange(self, fixture):
            self.request = {"session_id": session.ref.id, "turn_id": "root"}
            self.emit("status_running", {})
            self.emit(
                "usage",
                dict(
                    request_id="request-1",
                    model="gpt-6-luna",
                    input_tokens=10,
                    cached_input_tokens=0,
                    output_tokens=5,
                    reasoning_tokens=0,
                ),
            )
            return Scenario(
                SCOPE,
                SCOPE.model_copy(update={"tenant_id": "foreign"}),
                session,
                SessionSpec(
                    agent=ref("agent"),
                    agent_revision=Revision(local=1),
                    environment=ref("environment"),
                    config_revision=1,
                ),
            )

        def fault(self, name):
            if name == "observed_stop":
                self.finish("interrupted")
            elif name.startswith("admission_"):
                from mux.profiles.selfhosted import LOOP

                driver._profile = LOOP.model_copy(
                    update={
                        "support": {
                            **LOOP.support,
                            "memory_stores": name.removeprefix("admission_"),
                        }
                    }
                )
            else:
                raise AssertionError(name)

    from mux.contracts.ids import ResourceRef

    def ref(kind):
        return ResourceRef(
            id=kind,
            kind=kind,
            provider="selfhosted",
            account_scope_id="tenant",
            tenant_id=SCOPE.tenant_id,
            account_id=None,
        )

    fake = ConformanceHarness("running")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        result = await run_fixture(fixture_id, Adapter(driver, None, cast(ScriptedTransport, fake)))
        assert result.status == "pass", result.evidence
        assert result.evidence


async def test_same_operation_key_in_different_sessions_cannot_collide_at_harness():
    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, first = await setup(client, fake)
        spec = await driver.sessions.spec(SCOPE, first.ref)
        second = await driver.sessions.create(SCOPE, spec, key="second-session")
        left = await driver.events.send(SCOPE, first.ref, [MESSAGE], key="send")
        right = await driver.events.send(SCOPE, second.ref, [MESSAGE], key="send")
        assert first.ref != second.ref
        assert left.turn_id != right.turn_id
        assert fake.executions == 2
        assert len(await driver.usage.reconcile(SCOPE, first.ref)) == 1
        assert len(await driver.usage.reconcile(SCOPE, second.ref)) == 1


async def test_open_before_send_polls_until_current_turn_instead_of_treating_idle_as_eof():
    import asyncio

    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        source = await driver.events.open_stream(SCOPE, session.ref, after="0")
        pending = asyncio.create_task(anext(source))
        await asyncio.sleep(0)
        assert not pending.done()
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        first = await asyncio.wait_for(pending, timeout=1)
        assert first.type == "session.status_running"
        await source.aclose()


@pytest.mark.parametrize("decision", ["allow", "deny", "result"])
async def test_actions_use_explicit_decision_without_null_ambiguity(decision):
    from mux.contracts.actions import UserToolResult

    fake = Harness("approval")
    actions = []

    def handle(request):
        if request.url.path.endswith("/actions"):
            actions.append(json.loads(request.content))
        return fake.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        driver, session = await setup(client, fake)
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        action = (
            UserToolResult(
                action_id="approval", content=(TextPart(text="host executed"),), is_error=True
            )
            if decision == "result"
            else UserToolConfirmation(action_id="approval", decision=decision)
        )
        await driver.events.send(SCOPE, session.ref, [action], key="action")
        assert actions[0]["decision"] == decision
        assert ("result" in actions[0]) == (decision == "result")
        if decision == "result":
            assert actions[0]["result"] == {
                "content": [{"type": "text", "text": "host executed"}],
                "is_error": True,
            }


@pytest.mark.parametrize("foundry", [False, True])
async def test_mcp_credential_only_appears_in_turn_header_and_never_invocation_json(
    foundry, caplog
):
    import logging

    caplog.set_level(logging.DEBUG)
    credential = "Bearer test-per-turn-mcp-credential"
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = (
            FoundryInvocationsTransport(
                client,
                project_endpoint="https://harness/project",
                agent_name="agent",
                token="gateway-token",
            )
            if foundry
            else DirectTransport(
                client,
                base_url="https://harness",
                token="harness-token",
            )
        )
        await transport.request(
            "POST",
            "/v0/turns",
            after_seq=0,
            session_id="session",
            body={"session_id": "session"},
            mcp_authorization=credential,
        )
        await transport.request(
            "GET", "/v0/sessions/session/events?after_seq=0", after_seq=0, session_id="session"
        )
        await transport.request(
            "POST", "/v0/turns/turn/cancel", after_seq=0, session_id="session", body={}
        )
        assert requests[0].headers["X-Daimon-MCP-Authorization"] == credential
        assert all("X-Daimon-MCP-Authorization" not in request.headers for request in requests[1:])
        assert all(credential.encode() not in request.content for request in requests)
        assert credential not in caplog.text


@pytest.mark.parametrize("reserved_server", ["daimon-mcp", "other-server"])
async def test_host_tools_resolver_inlines_skills_and_seals_virtual_mcp_env(reserved_server):
    from types import SimpleNamespace

    from mux.drivers.selfhosted import wire
    from mux.errors import UnsupportedCapability

    async def tools(scope, spec):
        assert scope == SCOPE and spec.name == "test"
        return SimpleNamespace(
            mcp_servers=(
                wire.MCPServer(
                    name=reserved_server,
                    url="https://mcp",
                    headers_env={"Authorization": "DAIMON_MCP_AUTHORIZATION"},
                ),
            ),
            skills=(wire.InlineSkill(name="analysis", files={"SKILL.md": "analysis skill"}),),
        )

    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake, tools_resolver=tools)
        if reserved_server != "daimon-mcp":
            with pytest.raises(UnsupportedCapability, match="reserved_mcp_credential"):
                await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
            assert fake.executions == 0
        else:
            await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
            assert fake.request["mcp_servers"][0]["headers_env"] == {
                "Authorization": "DAIMON_MCP_AUTHORIZATION"
            }
            assert fake.request["skills"] == [
                wire.InlineSkill(name="analysis", files={"SKILL.md": "analysis skill"}).model_dump(
                    mode="json"
                )
            ]


async def test_accepted_turn_with_lost_ack_replays_without_second_execution_or_changed_intent():
    fake = Harness()
    dropped = False

    def handle(request):
        nonlocal dropped
        response = fake.handle(request)
        if request.method == "POST" and request.url.path == "/v0/turns" and not dropped:
            dropped = True
            raise httpx.ReadError("connection dropped after acceptance", request=request)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        driver, session = await setup(client, fake)
        with pytest.raises(ProviderError, match="transient_network"):
            await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        with pytest.raises(OperationConflict):
            await driver.events.send(
                SCOPE, session.ref, [UserMessage(content=(TextPart(text="changed"),))], key="send"
            )
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        assert fake.executions == 1
        assert len(await driver.usage.reconcile(SCOPE, session.ref)) == 1


@pytest.mark.parametrize(
    "override, deployment, expected",
    [(None, None, "gpt-6-luna"), ("gpt-6-luna", "gpt-6-luna", "gpt-6-luna"), (None, "wrong", None)],
)
async def test_agent_deployment_is_honored_and_environment_mismatch_refuses(
    override, deployment, expected
):
    from mux.errors import UnsupportedCapability

    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake, model=override, deployment=deployment)
        resources = driver.agents
        resources.value = resources.value.model_copy(
            update={
                "spec": resources.value.spec.model_copy(
                    update={
                        "model": ModelRef(
                            provider="selfhosted",
                            id="agent-deployment" if override else "gpt-6-luna",
                        )
                    }
                )
            }
        )
        if expected is None:
            with pytest.raises(UnsupportedCapability, match="model_deployment_mismatch"):
                await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
            assert fake.executions == 0
        else:
            await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
            assert fake.request["model"] == expected


async def test_multiple_message_deltas_and_snapshot_share_identity():
    from mux.contracts.events import AgentMessageDeltaPayload, AgentMessagePayload

    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        messages = [
            event
            async for event in await driver.events.open_stream(
                SCOPE, session.ref, after="0", previews=True
            )
            if event.type.startswith("agent.message")
        ]
        deltas = [AgentMessageDeltaPayload.model_validate(event.payload) for event in messages[:-1]]
        snapshot = AgentMessagePayload.model_validate(messages[-1].payload)
        assert len(deltas) == 2
        assert "".join(delta.text for delta in deltas) == "done"
        assert {delta.item_id for delta in deltas} == {snapshot.item_id}
        assert all(event.item_id == snapshot.item_id for event in messages)
        assert len({event.id for event in messages}) == 3


@pytest.mark.parametrize(
    "binary, per_turn", [(False, False), (True, False), (False, True), (True, True)]
)
async def test_decoded_skill_caps_refuse_before_any_post(binary, per_turn):
    import base64
    from types import SimpleNamespace

    from mux.drivers.selfhosted import wire
    from mux.errors import UnsupportedCapability

    data = b"a" * (wire.SKILL_MAX_BYTES if per_turn else wire.SKILL_MAX_BYTES + 1)

    async def tools(scope, agent):
        # model_construct also exercises the driver against a mutated/unvalidated bundle.
        return SimpleNamespace(
            mcp_servers=(),
            skills=tuple(
                wire.InlineSkill.model_construct(
                    name=f"skill{i}",
                    files={} if binary else {"SKILL.md": data.decode()},
                    binary_files={"asset.bin": base64.b64encode(data).decode()} if binary else {},
                    sha256="",
                )
                for i in range(4 if per_turn else 1)
            ),
        )

    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake, tools_resolver=tools)
        with pytest.raises((ValueError, UnsupportedCapability)):
            await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        assert fake.executions == 0


async def test_cancel_before_first_model_request_preserves_session():
    fake = Harness("pre_model")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        receipt = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="first")
        cancel = await driver.events.cancel(
            SCOPE, session.ref, turn_id=receipt.turn_id, key="cancel"
        )
        assert (
            await driver.events.wait_stopped(
                SCOPE, cancel, deadline=datetime.now(UTC) + timedelta(seconds=1)
            )
        ).stopped
        assert (
            await driver.events.wait_stopped(
                SCOPE, cancel, deadline=datetime.now(UTC) + timedelta(seconds=1)
            )
        ).outcome == "interrupted"
        assert receipt.turn_id in driver.events.unresolved_turns[session.ref]
        assert await driver.usage.reconcile(SCOPE, session.ref) == ()
        fake.mode = "complete"
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="second")
        assert (await driver.events.reconcile(SCOPE, session.ref)).state == "idle"
        assert len(await driver.usage.reconcile(SCOPE, session.ref)) == 1


@pytest.mark.parametrize("foundry", [False, True])
async def test_refreshing_harness_token_and_per_call_mcp_credentials(foundry):
    requests = []
    minted = 0

    async def token_provider():
        nonlocal minted
        minted += 1
        return f"fresh-{minted}"

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = (
            FoundryInvocationsTransport(
                client,
                project_endpoint="https://harness/project",
                agent_name="agent",
                token_provider=token_provider,
                api_version="custom",
            )
            if foundry
            else DirectTransport(client, base_url="https://harness", token_provider=token_provider)
        )
        for credential in ("Bearer caller-one", "Bearer caller-two"):
            await transport.request(
                "POST",
                "/v0/turns",
                after_seq=0,
                session_id="session",
                body={},
                mcp_authorization=credential,
            )
        await transport.request(
            "GET", "/v0/sessions/session/events?after_seq=9", after_seq=0, session_id="session"
        )
    assert [request.headers["authorization"] for request in requests] == [
        "Bearer fresh-1",
        "Bearer fresh-2",
        "Bearer fresh-3",
    ]
    assert [request.headers.get("X-Daimon-MCP-Authorization") for request in requests] == [
        "Bearer caller-one",
        "Bearer caller-two",
        None,
    ]
    assert all(b"caller-" not in request.content for request in requests)
    if foundry:
        assert all(
            request.url.query.decode()
            == f"api-version=custom&agent_session_id={sandbox_id('session')}&session_id=session"
            for request in requests
        )
        assert json.loads(requests[-1].content)["c3"]["query"] == {
            "after_seq": "9",
            "session_id": "session",
        }


@pytest.mark.parametrize("ack", [None, {}, {"accepted": True, "turn_id": "wrong"}, "not-json"])
async def test_ambiguous_ack_is_retryable_with_same_intent_and_one_execution(ack):
    fake = Harness()
    fail = True

    def handle(request):
        response = fake.handle(request)
        if request.url.path == "/v0/turns" and fail:
            return (
                httpx.Response(202, content=b"malformed")
                if ack == "not-json"
                else httpx.Response(202, json=ack)
            )
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        driver, session = await setup(client, fake)
        receipt = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        assert receipt.status == "outcome_unknown"
        fail = False
        retried = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        assert retried.turn_id == receipt.turn_id
        assert retried.status == "queued"
        assert fake.executions == 1


@pytest.mark.parametrize("status", [429, 502, 503, 504])
async def test_poll_retries_honor_retry_after_and_keep_cursor(status, monkeypatch):
    from mux.drivers.selfhosted import transport as module

    delays, requests = [], []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)

    def handle(request):
        requests.append(request)
        return (
            httpx.Response(status, headers={"Retry-After": "0.25"})
            if len(requests) == 1
            else httpx.Response(200, json={"events": [], "complete": False})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = DirectTransport(client, base_url="https://harness", token="token")
        await transport.request(
            "GET", "/v0/sessions/session/events?after_seq=12", after_seq=0, session_id="session"
        )
    assert delays == [0.25]
    assert [str(request.url) for request in requests] == [
        "https://harness/v0/sessions/session/events?after_seq=12&session_id=session"
    ] * 2
    response = httpx.Response(503)
    for attempt in range(8):
        assert 0.1 <= module.retry_delay(response, attempt) <= min(2, 0.1 * 2**attempt)


@pytest.mark.parametrize(
    "url, credential, allowed",
    [
        ("https://mcp", "OTHER_ENV", False),
        ("http://mcp", "HARNESS_MCP_CRED_SERVER", False),
        ("http://127.0.0.1/mcp", "HARNESS_MCP_CRED_SERVER", True),
        ("https://mcp", "HARNESS_MCP_CRED_SERVER", True),
    ],
)
async def test_mcp_credentials_require_reserved_prefix_and_secure_transport(
    url, credential, allowed
):
    from types import SimpleNamespace

    from mux.drivers.selfhosted import wire
    from mux.errors import UnsupportedCapability

    async def tools(scope, agent):
        return SimpleNamespace(
            mcp_servers=(
                wire.MCPServer(name="server", url=url, headers_env={"Authorization": credential}),
            ),
            skills=(),
        )

    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake, tools_resolver=tools)
        if allowed:
            await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        else:
            with pytest.raises(UnsupportedCapability):
                await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
            assert fake.executions == 0


@pytest.mark.parametrize("permission", ["auto", "ask"])
async def test_tool_policy_and_multiple_pending_approvals(permission):
    from mux.contracts.resources import ToolSpec

    fake = Harness("approval")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        resource = driver.agents
        resource.value = resource.value.model_copy(
            update={
                "spec": resource.value.spec.model_copy(
                    update={
                        "tools": tuple(
                            ToolSpec(
                                name=name,
                                kind="builtin",
                                permission=permission if name == "write" else "auto",
                            )
                            for name in ("bash", "read", "write", "edit")
                        )
                    }
                )
            }
        )
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        assert fake.request["tool_policy"] == {
            "bash": "auto",
            "read": "auto",
            "write": permission,
            "edit": "auto",
        }
        fake.emit(
            "requires_action",
            dict(action_id="second", call_id="second-call", name="write", input={}),
        )
        snapshot = await driver.events.reconcile(SCOPE, session.ref)
        assert {action.id for action in snapshot.required_actions} == {"approval", "second"}
        fake.emit("tool_result", dict(call_id="call", output="denied", is_error=True))
        snapshot = await driver.events.reconcile(SCOPE, session.ref)
        assert snapshot.state == "requires_action"
        assert [action.id for action in snapshot.required_actions] == ["second"]


@pytest.mark.parametrize(
    "url, allowed",
    [
        ("http://harness", False),
        ("http://localhost:8000", True),
        ("http://127.0.0.1:8000", True),
        ("http://[::1]:8000", True),
        ("https://harness", True),
    ],
)
async def test_bearer_transport_only_allows_plain_http_on_loopback(url, allowed):
    async with httpx.AsyncClient() as client:
        if allowed:
            DirectTransport(client, base_url=url, token="token")
        else:
            with pytest.raises(ValueError, match="HTTPS"):
                DirectTransport(client, base_url=url, token="token")


async def test_accepted_turn_has_active_identity_before_first_polled_event():
    fake = Harness("running")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        receipt = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        fake.events = []
        snapshot = await driver.events.reconcile(SCOPE, session.ref)
        assert snapshot.state == "running"
        assert snapshot.active_root_turn == receipt.turn_id


@pytest.mark.parametrize("foundry", [False, True])
async def test_reused_driver_sends_distinct_mcp_tokens_on_consecutive_turns(foundry):
    fake, headers = Harness(foundry=foundry), []

    def handle(request):
        body = json.loads(request.content) if request.content else None
        path = body["c3"]["path"] if foundry else request.url.path
        if path == "/v0/turns":
            headers.append(request.headers.get("X-Daimon-MCP-Authorization"))
        return fake.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        driver, session = await setup(client, fake)
        for key, credential in (("one", "Bearer caller-one"), ("two", "Bearer caller-two")):
            await driver.events.send(
                SCOPE, session.ref, [MESSAGE], key=key, mcp_authorization=credential
            )
            await driver.events.reconcile(SCOPE, session.ref)
        assert headers == ["Bearer caller-one", "Bearer caller-two"]
        assert fake.executions == 2
        assert all("caller-" not in json.dumps(body) for body in fake.requests.values())


async def test_polling_only_fetches_and_normalizes_new_cursor_events(monkeypatch):
    from mux.drivers.selfhosted import events as module

    fake, cursors, normalized = Harness(), [], []
    original = module.normalize

    def normalize(event, session):
        normalized.append(event.seq)
        return original(event, session)

    monkeypatch.setattr(module, "normalize", normalize)

    def handle(request):
        if request.method == "GET":
            cursors.append(request.url.params["after_seq"])
        return fake.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        driver, session = await setup(client, fake)
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="one")
        await driver.events.reconcile(SCOPE, session.ref)
        first_end = len(fake.events)
        await driver.events.reconcile(SCOPE, session.ref)
        assert cursors[-1] == str(first_end)
        assert normalized == list(range(1, first_end + 1))
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="two")
        await driver.events.reconcile(SCOPE, session.ref)
        assert normalized == list(range(1, len(fake.events) + 1))


async def test_two_pending_approvals_can_be_answered_independently():
    fake, decisions = Harness("approval"), []

    def handle(request):
        if request.url.path.endswith("/actions"):
            body = json.loads(request.content)
            decisions.append((body["action_id"], body["decision"]))
            call_id = "call" if body["action_id"] == "approval" else "second-call"
            fake.emit(
                "tool_result",
                dict(call_id=call_id, output=body["decision"], is_error=body["decision"] == "deny"),
            )
            if len(decisions) == 2:
                fake.finish()
            return httpx.Response(200, json={"ok": True})
        return fake.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        driver, session = await setup(client, fake)
        receipt = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        fake.emit(
            "requires_action",
            dict(action_id="second", call_id="second-call", name="write", input={}),
        )
        await driver.events.send(
            SCOPE,
            session.ref,
            [UserToolConfirmation(action_id="approval", decision="allow")],
            key="first-action",
            expected_turn=receipt.turn_id,
        )
        assert [
            action.id
            for action in (await driver.events.reconcile(SCOPE, session.ref)).required_actions
        ] == ["second"]
        await driver.events.send(
            SCOPE,
            session.ref,
            [UserToolConfirmation(action_id="second", decision="deny")],
            key="second-action",
            expected_turn=receipt.turn_id,
        )
        assert (await driver.events.reconcile(SCOPE, session.ref)).state == "idle"
        assert decisions == [("approval", "allow"), ("second", "deny")]


def test_retry_after_accepts_http_date_and_invalid_value_uses_backoff():
    from email.utils import format_datetime

    from mux.drivers.selfhosted.transport import retry_delay

    response = httpx.Response(
        429,
        headers={
            "Retry-After": format_datetime(datetime.now(UTC) + timedelta(seconds=60), usegmt=True)
        },
    )
    assert retry_delay(response, 0) == 30
    assert retry_delay(httpx.Response(503, headers={"Retry-After": "invalid"}), 0) == 0.1


async def test_default_stream_open_before_second_turn_never_replays_first_terminal():
    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        first = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="first")
        first_events = [
            event async for event in await driver.events.open_stream(SCOPE, session.ref)
        ]
        assert {event.turn_id for event in first_events} == {first.turn_id}
        source = await driver.events.open_stream(SCOPE, session.ref)
        second = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="second")
        values = [event async for event in source]
        assert values and values[-1].type == "session.turn_ended"
        assert {event.turn_id for event in values} == {second.turn_id}
        assert all(event.turn_id != first.turn_id for event in values)


async def test_renewed_grant_create_replays_durable_identity_and_conflicts_on_changed_spec():
    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        spec = await driver.sessions.spec(SCOPE, session.ref)
        renewed = SCOPE.model_copy(update={"authorization_id": "new-grant"})
        driver.events._c.authorization = lambda scope, kind, id_: scope.tenant_id == SCOPE.tenant_id
        assert (await driver.sessions.create(renewed, spec, key="create")).ref == session.ref
        fresh = SelfHostedDriver(
            driver.events._c.transport,
            account_scope_id="tenant",
            agents=driver.agents,
            environments=driver.environments,
            authorization=lambda *args: True,
            binding_lookup=lambda scope, id_: session.binding if id_ == session.ref.id else None,
        )
        replay = await fresh.sessions.create(renewed, spec, key="create")
        assert replay.binding == session.binding
        with pytest.raises(OperationConflict):
            await fresh.sessions.create(
                renewed, spec.model_copy(update={"config_revision": 2}), key="create"
            )


async def test_create_reconstruction_keeps_original_environment_pin(monkeypatch):
    from mux.errors import ContinuityLost

    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        spec = await driver.sessions.spec(SCOPE, session.ref)
        driver.environments.value = driver.environments.value.model_copy(
            update={"revision": Revision(local=2)}
        )
        fresh = SelfHostedDriver(
            driver.events._c.transport,
            account_scope_id="tenant",
            agents=driver.agents,
            environments=driver.environments,
            authorization=lambda *args: True,
            binding_lookup=lambda scope, id_: session.binding if id_ == session.ref.id else None,
        )
        replay = await fresh.sessions.create(SCOPE, spec, key="create")
        assert replay.binding == session.binding
        assert replay.binding.native_refs["environment_revision"] == "1"
        requests = []

        async def forbidden_transport(*args, **kwargs):
            requests.append((args, kwargs))
            raise AssertionError("changed environment must fail before credential/HTTP resolution")

        monkeypatch.setattr(driver.events._c.transport, "request", forbidden_transport)
        for call in (fresh.events.reconcile, fresh.sessions.retrieve, fresh.usage.reconcile):
            with pytest.raises(ContinuityLost, match="environment changed"):
                await call(SCOPE, replay.ref)
        assert requests == []
        with pytest.raises(ContinuityLost, match="environment changed"):
            await fresh.events.send(SCOPE, replay.ref, [MESSAGE], key="send")
        assert fake.executions == 0


@pytest.mark.parametrize("kind", ["agent", "environment"])
async def test_native_only_revision_change_breaks_continuity(kind):
    from mux.errors import ContinuityLost

    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, original = await setup(client, fake)
        for resources in (driver.agents, driver.environments):
            resources.value = resources.value.model_copy(
                update={"revision": Revision(local=1, native="v1")}
            )
        spec = (await driver.sessions.spec(SCOPE, original.ref)).model_copy(
            update={"agent_revision": Revision(local=1, native="v1")}
        )
        session = await driver.sessions.create(SCOPE, spec, key="native")
        resources = driver.agents if kind == "agent" else driver.environments
        resources.value = resources.value.model_copy(
            update={"revision": Revision(local=1, native="v2")}
        )
        with pytest.raises(ContinuityLost):
            await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        assert fake.executions == 0


@pytest.mark.parametrize("settled_immediately", [False, True])
async def test_lost_approval_ack_recovers_without_reexecuting(settled_immediately):
    fake, actions = Harness("approval"), []

    def result():
        fake.emit("tool_result", dict(call_id="call", output="executed", is_error=False))
        fake.finish()

    def handle(request):
        if request.url.path.endswith("/actions"):
            actions.append(json.loads(request.content))
            if settled_immediately:
                result()
            raise httpx.ReadError("lost action acknowledgement", request=request)
        return fake.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        driver, session = await setup(client, fake)
        turn = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        action = UserToolConfirmation(action_id="approval", decision="allow")
        receipt = await driver.events.send(
            SCOPE, session.ref, [action], key="approve", expected_turn=turn.turn_id
        )
        assert receipt.status == ("processed" if settled_immediately else "outcome_unknown")
        if not settled_immediately:
            result()
        retry = await driver.events.send(
            SCOPE, session.ref, [action], key="approve", expected_turn=turn.turn_id
        )
        assert retry.status == "processed"
        assert len(actions) == 1
        assert (
            await driver.events.send(
                SCOPE,
                session.ref,
                [action],
                key="same-action-other-key",
                expected_turn=turn.turn_id,
            )
        ).status == "processed"
        assert len(actions) == 1


@pytest.mark.parametrize("tools", [(), ("bash",), ("read", "write", "edit")])
async def test_narrowed_builtins_deny_omitted_tools(tools):
    from mux.contracts.resources import ToolSpec

    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        driver.agents.value = driver.agents.value.model_copy(
            update={
                "spec": driver.agents.value.spec.model_copy(
                    update={"tools": tuple(ToolSpec(name=name, kind="builtin") for name in tools)}
                )
            }
        )
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        assert fake.request["tool_policy"] == {
            name: "auto" if name in tools else "deny" for name in ("bash", "read", "write", "edit")
        }


@pytest.mark.parametrize(
    "policy, expected",
    [
        ({"write": "ask"}, {"server/write": "ask"}),
        ({"allowed_tools": ["read"]}, {"server/*": "deny", "server/read": "auto"}),
        ({"allowed_tools": ["read"], "read": "ask"}, {"server/*": "deny", "server/read": "ask"}),
        ({"server/write": "deny", "*": "ask"}, {"server/write": "deny", "server/*": "ask"}),
    ],
)
async def test_mcp_policy_uses_server_namespace_and_deny_allowlist(policy, expected):
    from mux.contracts.resources import MCPConnection

    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        driver.agents.value = driver.agents.value.model_copy(
            update={
                "spec": driver.agents.value.spec.model_copy(
                    update={
                        "mcp_servers": (
                            MCPConnection(name="server", url="https://mcp", tool_policy=policy),
                        )
                    }
                )
            }
        )
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        assert fake.request["tool_policy"] == expected


async def test_unknown_turn_ack_still_has_active_identity_and_can_cancel():
    fake = Harness("running")

    def handle(request):
        if request.url.path == "/v0/turns":
            fake.request = json.loads(request.content)
            return httpx.Response(202, json={})
        return fake.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        driver, session = await setup(client, fake)
        receipt = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        assert receipt.status == "outcome_unknown"
        assert (
            await driver.events.reconcile(SCOPE, session.ref)
        ).active_root_turn == receipt.turn_id
        assert (
            await driver.events.cancel(SCOPE, session.ref, turn_id=receipt.turn_id, key="cancel")
        ).status == "requested"


async def test_poll_network_error_retries_and_retry_after_is_bounded(monkeypatch):
    from mux.drivers.selfhosted import transport as module

    calls, delays = [], []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = DirectTransport(client, base_url="https://harness", token="token")
        await transport.request(
            "GET", "/v0/sessions/session/events?after_seq=5", after_seq=0, session_id="session"
        )
    assert len(calls) == 2 and delays == [0.1]
    assert module.retry_delay(httpx.Response(429, headers={"Retry-After": "999999"}), 0) == 30
    assert module.retry_delay(httpx.Response(429, headers={"Retry-After": "inf"}), 0) == 0.1


async def test_retry_after_never_exceeds_request_deadline(monkeypatch):
    from mux.drivers.selfhosted import transport as module

    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return (
            httpx.Response(429, headers={"Retry-After": "9999"})
            if count == 1
            else httpx.Response(200, json={})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = DirectTransport(client, base_url="https://harness", token="token")
        await transport.request(
            "GET",
            "/v0/sessions/session/events",
            after_seq=0,
            session_id="session",
            deadline=datetime.now(UTC) + timedelta(seconds=0.5),
        )
    assert len(delays) == 1 and 0 < delays[0] <= 0.5


async def test_wait_stopped_uses_exponential_backoff_bounded_by_deadline(monkeypatch):
    from mux.contracts.receipts import CancelReceipt
    from mux.drivers.selfhosted import events as module

    fake = Harness("running")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        turn = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        current, delays = datetime.now(UTC), []

        class Clock:
            @staticmethod
            def now(tz):
                return current

        async def sleep(delay):
            nonlocal current
            delays.append(delay)
            current += timedelta(seconds=delay)

        monkeypatch.setattr(module, "datetime", Clock)
        monkeypatch.setattr(module.asyncio, "sleep", sleep)
        monkeypatch.setattr(module.random, "uniform", lambda low, high: high)
        receipt = CancelReceipt(
            operation_id="cancel",
            session=session.ref,
            turn_id=turn.turn_id,
            status="requested",
            requested_at=current,
        )
        result = await driver.events.wait_stopped(
            SCOPE, receipt, deadline=current + timedelta(seconds=5)
        )
        assert not result.stopped
        assert delays[:5] == [0.1, 0.2, 0.4, 0.8, 1.6]
        assert max(delays) <= 2 and sum(delays) == pytest.approx(5)


@pytest.mark.parametrize("permission", ["auto", "ask"])
async def test_mcp_toolset_named_write_cannot_overwrite_builtin_approval(permission):
    from mux.contracts.resources import MCPConnection, ToolSpec

    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        driver.agents.value = driver.agents.value.model_copy(
            update={
                "spec": driver.agents.value.spec.model_copy(
                    update={
                        "tools": (
                            ToolSpec(name="write", kind="builtin", permission="ask"),
                            ToolSpec(name="write", kind="mcp_toolset", permission=permission),
                        ),
                        "mcp_servers": (MCPConnection(name="write", url="https://mcp"),),
                    }
                )
            }
        )
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        assert fake.request["tool_policy"] == {
            "bash": "deny",
            "read": "deny",
            "edit": "deny",
            "write": "ask",
            "write/*": permission,
        }


async def test_agent_mcp_credential_requires_scoped_resolver_before_any_harness_io():
    from mux.contracts.resources import MCPConnection
    from mux.errors import UnsupportedCapability

    calls = []
    fake = Harness()

    def handle(request):
        calls.append(request)
        return fake.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        driver, session = await setup(client, fake)
        driver.agents.value = driver.agents.value.model_copy(
            update={
                "spec": driver.agents.value.spec.model_copy(
                    update={
                        "mcp_servers": (
                            MCPConnection(
                                name="server",
                                url="https://attacker.example",
                                credential_ref="HARNESS_MCP_CRED_PRIVATE",
                            ),
                        ),
                    }
                )
            }
        )
        with pytest.raises(
            UnsupportedCapability, match="credentialed_mcp_requires_scoped_tools_resolver"
        ):
            await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        assert calls == []


@pytest.mark.parametrize("retry_key", ["approve", "another-operation"])
async def test_approval_reposts_identical_decision_after_one_connect_error(retry_key):
    fake, actions = Harness("approval"), []

    def handle(request):
        if request.url.path.endswith("/actions"):
            actions.append(json.loads(request.content))
            if len(actions) == 1:
                raise httpx.ConnectError("temporarily unavailable", request=request)
            fake.emit("tool_result", dict(call_id="call", output="executed", is_error=False))
            fake.finish()
            return httpx.Response(200, json={"accepted": True})
        return fake.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        driver, session = await setup(client, fake)
        turn = await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        action = UserToolConfirmation(action_id="approval", decision="allow")
        first = await driver.events.send(
            SCOPE, session.ref, [action], key="approve", expected_turn=turn.turn_id
        )
        assert first.status == "outcome_unknown"
        assert (await driver.events.reconcile(SCOPE, session.ref)).state == "requires_action"
        with pytest.raises(OperationConflict):
            await driver.events.send(
                SCOPE,
                session.ref,
                [UserToolConfirmation(action_id="approval", decision="deny")],
                key="conflicting-decision",
                expected_turn=turn.turn_id,
            )
        retried = await driver.events.send(
            SCOPE, session.ref, [action], key=retry_key, expected_turn=turn.turn_id
        )
        assert retried.status == "queued"
        assert actions == [
            {"action_id": "approval", "decision": "allow"},
            {"action_id": "approval", "decision": "allow"},
        ]
        assert (await driver.events.reconcile(SCOPE, session.ref)).state == "idle"
        assert (
            await driver.events.send(
                SCOPE, session.ref, [action], key="settled-retry", expected_turn=turn.turn_id
            )
        ).status == "processed"
        assert len(actions) == 2


async def test_idle_stream_can_outlive_turn_limit_and_receive_later_completion(monkeypatch):
    from mux.drivers.selfhosted import events as module

    fake = Harness("running")
    current = started = datetime.now(UTC)

    class Clock:
        @staticmethod
        def now(tz):
            return current

    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake, deadline_s=0.3)
        source = await driver.events.open_stream(SCOPE, session.ref)

        async def sleep(delay):
            nonlocal current
            current += timedelta(seconds=1)
            if fake.request is None:
                await driver.events.send(SCOPE, session.ref, [MESSAGE], key="later-turn")
            elif fake.events[-1]["type"] != "turn_ended":
                fake.finish()

        monkeypatch.setattr(module, "datetime", Clock)
        monkeypatch.setattr(module.asyncio, "sleep", sleep)
        values = [event async for event in source]
        assert current - started == timedelta(seconds=2)
        assert values[-1].type == "session.turn_ended"


@pytest.mark.parametrize(
    "policy",
    [
        {"allowed_tools": ["read"], "write": "auto"},
        {"allowed_tools": [], "*": "auto"},
        {"other/write": "ask"},
        {"allowed_tools": "read"},
        {"allowed_tools": ["../read"]},
        {"write": "sometimes"},
    ],
)
async def test_mcp_invalid_or_widening_policy_refuses_before_harness_io(policy):
    from mux.contracts.resources import MCPConnection
    from mux.errors import UnsupportedCapability

    fake = Harness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        driver.agents.value = driver.agents.value.model_copy(
            update={
                "spec": driver.agents.value.spec.model_copy(
                    update={
                        "mcp_servers": (
                            MCPConnection(
                                name="server",
                                url="https://mcp",
                                tool_policy=policy,
                            ),
                        ),
                    }
                )
            }
        )
        with pytest.raises(UnsupportedCapability):
            await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        assert fake.executions == 0


@pytest.mark.parametrize("foundry", [False, True])
async def test_transport_refuses_an_omitted_recovery_fence_before_http(foundry):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"cancelled": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = (
            FoundryInvocationsTransport(
                client, project_endpoint="https://project", agent_name="agent", token="token"
            )
            if foundry
            else DirectTransport(client, base_url="https://harness", token="token")
        )
        with pytest.raises(TypeError, match="after_seq"):
            await transport.request("POST", "/v0/turns/turn/cancel", session_id="session")
    assert calls == []
