from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mux.contracts.actions import NativeInput, UserMessage, UserToolConfirmation, UserToolResult
from mux.contracts.config import (
    BackendConfig,
    CapabilityRequirement,
    ConfigRevision,
    resolve_default,
)
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, PageRequest, Revision
from mux.contracts.ports import ManagedAgents, Steering
from mux.contracts.resources import AgentFilter, AgentPatch, AgentSpec, EnvironmentSpec, SessionSpec
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import (
    ContinuityLost,
    ExtensionVersionError,
    MigrationUnsupported,
    ProviderError,
    ScopeViolation,
    UnsupportedCapability,
)

from .conftest import REF, SCOPE, FakeTransport, event, native_session, page, turn


def config(**values: object) -> ConfigRevision:
    return ConfigRevision.create(
        ChannelRef(tenant_id="t", platform="discord", channel_id="c"),
        1,
        resolve_default(
            BackendConfig.model_validate(
                {
                    "backend": "openai",
                    "profile": "openai.persistent_workspace",
                    "model": "fixture",
                    **values,
                }
            )
        ),
    )


def test_profiles_admission_default_and_private_ports(driver: OpenAIDriver) -> None:
    protocol: ManagedAgents = driver
    assert protocol.capabilities().profile_id == "openai.persistent_workspace"
    assert protocol.capabilities().core
    admission = protocol.admit(config())
    assert admission.provider == "openai"
    assert admission.waived_core == ()
    with pytest.raises(UnsupportedCapability, match="memory_stores"):
        driver.admit(config(requires={"memory_stores": CapabilityRequirement(level="required")}))
    assert resolve_default(BackendConfig()).backend == "anthropic"
    assert resolve_default(BackendConfig()).profile == "anthropic.managed_agents"
    assert resolve_default(BackendConfig()).thread_mode == "per_caller"
    for target in (driver, driver.events, driver.sessions):
        for name in ("raw_client", "client", "sdk", "raw"):
            assert getattr(target, name, None) is None
    assert driver.extension(Steering, namespace="openai.steer", version=1) is driver.events
    with pytest.raises(ExtensionVersionError):
        driver.extension(Steering, namespace="openai.steer", version=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["empty_input", "page_limit"])
async def test_caller_validation_is_invalid_request_before_provider_io(
    driver: OpenAIDriver, transport: FakeTransport, case: str
) -> None:
    with pytest.raises(ProviderError) as error:
        if case == "empty_input":
            await driver.events.send(SCOPE, REF, (), key="empty")
        else:
            await driver.agents.list(SCOPE, filters=AgentFilter(), page=PageRequest(limit=101))
    assert error.value.category == "invalid_request"
    assert error.value.native_code == case
    assert not transport.calls


@pytest.mark.parametrize(
    "capability", ["multiagent", "workspace_export_import", "session_resources"]
)
@pytest.mark.parametrize("profile_id", ["openai.persistent_workspace", "openai.conversation_only"])
def test_unimplemented_features_refuse_required_admission(
    transport: FakeTransport, capability: str, profile_id: str
) -> None:
    driver = OpenAIDriver(
        transport,
        account_scope_id="project",
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
        profile_id=profile_id,
    )
    with pytest.raises(UnsupportedCapability) as refusal:
        driver.admit(
            config(
                profile=profile_id,
                requires={capability: CapabilityRequirement(level="required")},
            )
        )
    assert capability in refusal.value.missing
    assert transport.calls == []


@pytest.mark.asyncio
async def test_send_202_queued_and_no_invented_input_ids(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    receipt = await driver.events.send(
        SCOPE, REF, (UserMessage(content=(TextPart(text="hello"),)),), key="key"
    )
    assert receipt.status == "queued" and receipt.input_ids == () and receipt.turn_id is None
    assert transport.calls[-1][2] == {
        "events": [
            {
                "type": "agent.session.input.message",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
            }
        ],
    }
    assert transport.request_keys[-1] == "key"
    transport.responses["POST", "/agents/sessions/s/events"] = ProviderError(
        "transient_network", retryable=True
    )
    receipt = await driver.events.send(
        SCOPE, REF, (UserMessage(content=(TextPart(text="hello"),)),), key="unknown"
    )
    assert receipt.status == "outcome_unknown"
    assert sum(method == "POST" for method, *_ in transport.calls) == 2


@pytest.mark.asyncio
async def test_refusals_before_writes_and_foreign_scope_before_reads(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    for input_ in (
        UserToolConfirmation(action_id="call", decision="allow"),
        NativeInput(extension=ExtensionConfig(namespace="openai.bypass", version=1, value={})),
    ):
        with pytest.raises(UnsupportedCapability):
            await driver.events.send(SCOPE, REF, (input_,), key="bad")
    with pytest.raises(UnsupportedCapability):
        await driver.events.send(SCOPE, REF, (UserMessage(content=(), mode="steer"),), key="bad")
    before = len(transport.calls)
    with pytest.raises(ScopeViolation):
        await driver.sessions.retrieve(SCOPE.model_copy(update={"tenant_id": "other"}), REF)
    assert len(transport.calls) == before and all(c[0] == "GET" for c in transport.calls)


@pytest.mark.asyncio
async def test_tool_result_routes_call_and_turn_from_required_action(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    native = native_session("requires_action")
    native["required_actions"] = [
        {
            "type": "function_call",
            "call_id": "call",
            "turn_id": "root",
            "name": "tool",
            "arguments": {},
        }
    ]
    transport.responses["GET", "/agents/sessions/s"] = native
    transport.responses["GET", "/agents/sessions/s/turns"] = page(turn("waiting"))
    await driver.events.send(
        SCOPE,
        REF,
        (UserToolResult(action_id="call", content=(TextPart(text="result"),)),),
        key="result",
    )
    assert transport.calls[-1][2] == {
        "events": [
            {
                "type": "agent.session.input.tool_result",
                "call_id": "call",
                "turn_id": "root",
                "success": True,
                "output": [{"type": "input_text", "text": "result"}],
            }
        ],
    }
    assert transport.request_keys[-1] == "result"


@pytest.mark.asyncio
async def test_stream_opens_before_iteration_and_closes(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    transport.stream_values = [event("turn.completed", "e", turn=turn())]
    stream = await driver.events.open_stream(SCOPE, REF)
    assert len(transport.sources) == 1
    assert len([e async for e in stream]) == 1
    assert transport.sources[0].closed
    with pytest.raises(UnsupportedCapability):
        await driver.events.open_stream(SCOPE, REF, after="cursor")


@pytest.mark.asyncio
async def test_cancel_ack_does_not_stop_and_deadline_or_child_cannot_claim_stop(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    transport.responses["GET", "/agents/sessions/s"] = native_session("in_progress")
    transport.responses["GET", "/agents/sessions/s/turns"] = page(turn("in_progress"))
    transport.responses["GET", "/agents/sessions/s/turns/root"] = turn("in_progress")
    receipt = await driver.events.cancel(SCOPE, REF, turn_id="root", key="cancel")
    assert receipt.status == "requested"
    assert transport.calls[-1][2] == {
        "events": [{"type": "agent.session.input.cancel"}],
    }
    assert transport.request_keys[-1] == "cancel"
    stop = await driver.events.wait_stopped(
        SCOPE, receipt, deadline=datetime.now(UTC) + timedelta(seconds=1)
    )
    assert not stop.stopped and stop.outcome is None
    transport.responses["GET", "/agents/sessions/s/turns/root"] = turn("cancelled")
    assert (
        await driver.events.wait_stopped(
            SCOPE, receipt, deadline=datetime.now(UTC) + timedelta(seconds=1)
        )
    ).outcome == "interrupted"
    transport.responses["GET", "/agents/sessions/s/turns/root"] = turn("completed", child="sub")
    with pytest.raises(ProviderError):
        await driver.events.wait_stopped(
            SCOPE, receipt, deadline=datetime.now(UTC) + timedelta(seconds=1)
        )
    calls = len(transport.calls)
    assert not (
        await driver.events.wait_stopped(SCOPE, receipt, deadline=datetime.now(UTC))
    ).stopped
    assert len(transport.calls) == calls


@pytest.mark.asyncio
async def test_continuity_never_silently_creates_and_binding_identity_survives(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    good = await driver.sessions.retrieve(SCOPE, REF)
    assert (
        good.continuity.workspace == "native_reuse"
        and good.binding.native_refs["environment"] == "env"
    )
    native = native_session()
    native["environment"] = {"type": "none"}
    transport.responses["GET", "/agents/sessions/s"] = native
    with pytest.raises(ContinuityLost) as exc:
        await driver.sessions.retrieve(SCOPE, REF)
    assert exc.value.binding_id == good.binding.id
    transport.responses["GET", "/agents/sessions/s"] = ProviderError("not_found", retryable=False)
    with pytest.raises(ContinuityLost):
        await driver.sessions.retrieve(SCOPE, REF)
    assert all(call[0] == "GET" for call in transport.calls)


@pytest.mark.asyncio
async def test_conversation_only_explicit_opt_in_and_no_hidden_environment(
    transport: FakeTransport,
) -> None:
    driver = OpenAIDriver(
        transport,
        account_scope_id="project",
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
        profile_id="openai.conversation_only",
        authorization=lambda s, k, i: s == SCOPE,
    )
    admission = driver.admit(config(profile="openai.conversation_only"))
    assert "thread_workspace_persistence" in admission.waived_core
    native = native_session()
    native["environment"] = {"type": "none"}
    transport.responses["POST", "/agents/sessions"] = native
    spec = SessionSpec(
        agent=REF.model_copy(update={"kind": "agent", "id": "a"}),
        agent_revision=Revision(local=0),
        config_revision=1,
    )
    with pytest.raises(ProviderError) as missing:
        await driver.sessions.create(SCOPE, spec, key="missing")
    assert missing.value.category == "invalid_request"
    assert missing.value.native_code == "initial_input_required" and not transport.calls
    session = await driver.sessions.create(
        SCOPE, spec, key="create", initial=UserMessage(content=(TextPart(text="hello"),))
    )
    assert session.continuity.workspace == "none"
    body = transport.calls[-1][2]
    assert body is not None and body["environment"] == {"type": "none"}
    assert body["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}]
    with pytest.raises(UnsupportedCapability):
        await driver.sessions.create(
            SCOPE,
            spec.model_copy(update={"environment": REF.model_copy(update={"kind": "environment"})}),
            key="bad",
        )


@pytest.mark.asyncio
async def test_agents_templates_omission_empty_and_session_hosted_mode(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    transport.responses["POST", "/agents"] = {
        "id": "agent",
        "created_at": 0,
        "model": "fixture",
        "name": "test",
        "tools": [],
        "metadata": {"mux_tenant": "t"},
    }
    agent = await driver.agents.create(
        SCOPE, AgentSpec(name="test", model=ModelRef(provider="openai", id="fixture")), key="agent"
    )
    body = transport.calls[-1][2]
    assert body is not None and "tools" not in body and "instructions" not in body
    await driver.agents.create(
        SCOPE,
        AgentSpec(name="test", model=ModelRef(provider="openai", id="fixture"), tools=()),
        key="empty",
    )
    body = transport.calls[-1][2]
    assert body is not None and body["tools"] == []
    transport.responses["POST", "/agents/environments/templates"] = {
        "id": "template",
        "name": "test",
        "created_at": 0,
    }
    env = await driver.environments.create(SCOPE, EnvironmentSpec(name="test"), key="template")
    transport.responses["POST", "/agents/sessions"] = native_session()
    await driver.sessions.create(
        SCOPE,
        SessionSpec(
            agent=agent.ref,
            agent_revision=Revision(local=0),
            environment=env.ref,
            config_revision=1,
        ),
        key="s",
    )
    body = transport.calls[-1][2]
    assert body is not None and body["environment"] == {
        "type": "openai_hosted",
        "environment_template_id": "template",
    }
    with pytest.raises(UnsupportedCapability):
        await driver.agents.update(
            SCOPE, agent.ref, AgentPatch(), expected=agent.revision, key="cas"
        )
    with pytest.raises(MigrationUnsupported):
        await driver.sessions.migrate(SCOPE, REF, config(), expected=0, key="migration")


@pytest.mark.asyncio
async def test_host_binding_id_and_environment_continuity_are_authoritative(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    original = await driver.sessions.retrieve(SCOPE, REF)
    binding = original.binding.model_copy(update={"id": "stable-host-binding", "generation": 3})
    restarted = OpenAIDriver(
        transport,
        account_scope_id="project",
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
        authorization=lambda s, k, i: s == SCOPE,
        binding_lookup=lambda s, i: binding,
    )
    assert (await restarted.sessions.retrieve(SCOPE, REF)).binding == binding
    native = native_session()
    native["environment"] = {"type": "openai_hosted", "id": "replacement"}
    transport.responses["GET", "/agents/sessions/s"] = native
    before = len(transport.calls)
    with pytest.raises(ContinuityLost) as exc:
        await restarted.sessions.retrieve(SCOPE, REF)
    assert exc.value.binding_id == "stable-host-binding" and len(transport.calls) == before + 1


@pytest.mark.asyncio
async def test_missing_host_authorization_and_forged_grant_are_refused(
    transport: FakeTransport,
) -> None:
    driver = OpenAIDriver(
        transport,
        account_scope_id="project",
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
    )
    with pytest.raises(ScopeViolation):
        await driver.sessions.retrieve(SCOPE, REF)
    assert not transport.calls
    for ref in (
        REF.model_copy(update={"provider": "anthropic"}),
        REF.model_copy(update={"account_scope_id": "other"}),
        REF.model_copy(update={"account_id": "other"}),
    ):
        with pytest.raises(ScopeViolation):
            await driver.sessions.retrieve(SCOPE, ref)
    assert not transport.calls


@pytest.mark.asyncio
async def test_foreign_native_turn_cannot_report_already_stopped(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    native = turn()
    native["session_id"] = "foreign"
    transport.responses["GET", "/agents/sessions/s/turns/root"] = native
    with pytest.raises(ProviderError):
        await driver.events.cancel(SCOPE, REF, turn_id="root", key="cancel")
    transport.responses["GET", "/agents/sessions/s"] = native_session("in_progress")
    transport.responses["GET", "/agents/sessions/s/turns"] = page(native)
    with pytest.raises(ProviderError):
        await driver.sessions.retrieve(SCOPE, REF)
    assert all(call[0] == "GET" for call in transport.calls)
