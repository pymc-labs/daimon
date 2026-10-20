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
                if self.mode != "unmetered":
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
                elif self.mode != "running":
                    self.emit("agent_message_delta", {"text": "done"})
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
            account_id=SCOPE.account_id,
        )

    agents = cast(
        Agents,
        Resources(
            ref("agent"),
            Agent,
            AgentSpec(
                name="test",
                model=ModelRef(provider="selfhosted", id="gpt-5"),
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
        source = await driver.events.open_stream(SCOPE, session.ref)
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


async def test_unmetered_completion_is_refused():
    fake = Harness("unmetered")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        driver, session = await setup(client, fake)
        await driver.events.send(SCOPE, session.ref, [MESSAGE], key="send")
        with pytest.raises(ProviderError, match="turn_usage_required"):
            await driver.events.open_stream(SCOPE, session.ref)


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
            await driver.models.validate(SCOPE, ModelRef(provider="selfhosted", id="gpt-5"))
        ).admitted
        assert not (
            await driver.models.validate(SCOPE, ModelRef(provider="openai", id="gpt-5"))
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
        fake.emit("model_request_started", {"request_id": "request-1", "model": "gpt-5"})
        pending = await driver.usage.reconcile(SCOPE, session.ref)
        assert len(pending) == 1
        assert pending[0].completeness == "unknown"
        assert pending[0].input_tokens is None and pending[0].output_tokens is None
        fake.emit(
            "usage",
            dict(
                request_id="request-1",
                model="gpt-5",
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
        fake.emit("model_request_started", {"request_id": "crash-window", "model": "gpt-5"})
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
                    model="gpt-5",
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
            account_id=SCOPE.account_id,
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
        source = await driver.events.open_stream(SCOPE, session.ref)
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
                mcp_authorization=credential,
            )
            if foundry
            else DirectTransport(
                client,
                base_url="https://harness",
                token="harness-token",
                mcp_authorization=credential,
            )
        )
        await transport.request(
            "POST", "/v0/turns", session_id="session", body={"session_id": "session"}
        )
        await transport.request(
            "GET", "/v0/sessions/session/events?after_seq=0", session_id="session"
        )
        await transport.request("POST", "/v0/turns/turn/cancel", session_id="session", body={})
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
                {"name": "analysis", "files": {"SKILL.md": "analysis skill"}}
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
