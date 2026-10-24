from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import cast

import pytest
from mux.contracts.actions import InputEvent, UserMessage
from mux.contracts.admission import Admission
from mux.contracts.config import CapabilityRequirement, ConfigRevision, ResolvedBackend
from mux.contracts.events import TextPart
from mux.contracts.ids import ChannelRef, ResourceRef, Scope
from mux.contracts.profile import Capability, Support
from mux.contracts.receipts import CancelReceipt, SendReceipt, StopObservation
from mux.contracts.resources import Session
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.normalize import EventNormalizer
from mux.drivers.openai.sessions import OpenAISessions
from mux.drivers.openai.transport import Object
from mux.drivers.openai.turn import OpenAIEvents
from mux.errors import ProviderError, UnsupportedCapability
from mux.testing.conformance import Registry, run
from mux.testing.conformance.fixtures import FIXTURES
from mux.testing.conformance.runner import Adapter, ConformanceFailure, PendingKind, run_fixture
from mux.testing.drivers.openai.conformance import (
    PENDING,
    SCRIPTED_FIXTURES,
    OpenAIOfflineFactory,
    OpenAIScriptedTransport,
    register,
)
from mux.testing.drivers.openai.conformance_state import OpenAIConformanceEvents
from mux.testing.isolated_sdk import build_async_openai


@pytest.fixture
async def adapters() -> AsyncIterator[OpenAIOfflineFactory]:
    async with OpenAIOfflineFactory(sdk_factory=build_async_openai) as factory:
        yield factory


def script(adapter: Adapter) -> OpenAIScriptedTransport:
    assert isinstance(adapter.transport, OpenAIScriptedTransport)
    return adapter.transport


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", sorted(SCRIPTED_FIXTURES))
async def test_real_driver_passes_shared_probe(
    adapters: OpenAIOfflineFactory, fixture: str
) -> None:
    adapter = adapters()
    result = await run_fixture(fixture, adapter)
    assert result.status == "pass" and result.evidence
    wire = script(adapter)
    if fixture == "C05":
        assert any(request.url.params.get("after") == "item" for request in wire.requests)
        assert len(wire.streams) == 2
        assert all(source.closed.is_set() for source in wire.streams)
    if fixture == "C06":
        assert wire.mutation_count == 1
        post = next(request for request in wire.requests if request.method == "POST")
        assert post.headers["Idempotency-Key"] == "cancel"


@pytest.mark.asyncio
async def test_matrix_pins_every_pass_and_typed_pending_reason() -> None:
    registry = Registry()
    async with register(registry, sdk_factory=build_async_openai):
        assert registry.names == ("openai.offline",)
        results = await run(registry, "openai.offline")
        assert [r.fixture_id for r in results] == [f"C{i:02}" for i in range(1, 19)]
        assert {r.fixture_id for r in results if r.status == "pass"} == SCRIPTED_FIXTURES
        assert {r.fixture_id for r in results if r.status == "pending"} == set(PENDING)
        assert all(r.status in ("pass", "pending") and r.evidence for r in results)
        for result in results:
            if result.fixture_id in PENDING:
                assert result.pending_reason == PENDING[result.fixture_id]
        assert PENDING["C08"].kind == PendingKind.CAPABILITY_UNAVAILABLE
        assert all(
            PENDING[id_].kind == PendingKind.ADAPTER_DEPENDENCY for id_ in PENDING if id_ != "C08"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", sorted(PENDING))
async def test_pending_is_explicit_and_precedes_native_io(
    adapters: OpenAIOfflineFactory, fixture: str
) -> None:
    adapter = adapters()
    result = await run_fixture(fixture, adapter)
    assert result.status == "pending" and result.pending_reason == PENDING[fixture]
    assert script(adapter).requests == []


@pytest.mark.asyncio
async def test_registration_has_no_discovery_and_probes_are_fresh() -> None:
    registry = Registry()
    async with register(registry, sdk_factory=build_async_openai):
        first, second = registry.create("openai.offline"), registry.create("openai.offline")
        assert first.driver is not second.driver and first.transport is not second.transport
        assert script(first).requests == script(second).requests == []
        await run_fixture("C05", first)
        assert script(second).requests == []
    assert all(source.closed.is_set() for source in script(first).streams)
    assert script(first).closed and script(second).closed


@pytest.mark.asyncio
async def test_stream_preserves_preview_authority_and_eof_occupancy(
    adapters: OpenAIOfflineFactory,
) -> None:
    adapter = adapters()
    scenario = await adapter.transport.arrange("C05")
    events = [
        e
        async for e in adapter.driver.events.stream(
            scenario.scope, scenario.session.ref, previews=True
        )
    ]
    assert any(e.type == "agent.message.delta" and e.authority == "preview" for e in events)
    assert not any(e.type == "session.turn_ended" for e in events)
    current = await adapter.driver.sessions.retrieve(scenario.scope, scenario.session.ref)
    assert current.state == "running" and current.active_root_turn == "root"


@pytest.mark.asyncio
async def test_cancel_acceptance_then_empty_stream_does_not_prove_stop(
    adapters: OpenAIOfflineFactory,
) -> None:
    adapter = adapters()
    scenario = await adapter.transport.arrange("C06")
    receipt = await adapter.driver.events.cancel(
        scenario.scope, scenario.session.ref, turn_id="root", key="cancel"
    )
    assert receipt.status == "requested"
    events = [e async for e in adapter.driver.events.stream(scenario.scope, scenario.session.ref)]
    assert not any(e.type == "session.turn_ended" for e in events)
    observed = await adapter.driver.events.wait_stopped(
        scenario.scope, receipt, deadline=datetime.now(UTC)
    )
    assert not observed.stopped and observed.outcome is None
    current = await adapter.driver.sessions.retrieve(scenario.scope, scenario.session.ref)
    assert current.state == "running" and current.active_root_turn == "root"
    assert all(source.closed.is_set() for source in script(adapter).streams)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability,support", [("native_event_replay", "unsupported"), ("memory_stores", "unknown")]
)
async def test_fixed_profile_refuses_actual_unsupported_and_unknown_entries(
    adapters: OpenAIOfflineFactory, capability: Capability, support: Support
) -> None:
    adapter = adapters()
    profile = adapter.driver.capabilities()
    assert profile.support_for(capability) == support
    config = ConfigRevision.create(
        ChannelRef(tenant_id="tenant", platform="offline", channel_id="channel"),
        1,
        ResolvedBackend(
            backend="openai",
            profile=profile.profile_id,
            model="fixture",
            requires={capability: CapabilityRequirement(level="required")},
        ),
    )
    with pytest.raises(UnsupportedCapability) as refused:
        adapter.driver.admit(config)
    assert capability in refused.value.missing
    assert script(adapter).requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,diagnostic",
    [
        ("message", "saved message identity or text was altered"),
        ("function_call_output", "saved tool result content was altered"),
    ],
)
async def test_content_corruption_mutants_fail_fixture_checks(
    adapters: OpenAIOfflineFactory, monkeypatch: pytest.MonkeyPatch, kind: str, diagnostic: str
) -> None:
    original = EventNormalizer.saved_item

    def corrupt(self: EventNormalizer, item: Object):
        if item.get("type") == kind:
            item = (
                {**item, "content": [{"type": "output_text", "text": "corrupted"}]}
                if kind == "message"
                else {**item, "output": "corrupted"}
            )
        return original(self, item)

    monkeypatch.setattr(EventNormalizer, "saved_item", corrupt)
    adapter = adapters()
    with pytest.raises(ConformanceFailure, match=diagnostic):
        await FIXTURES["C05"](adapter.driver, adapter.store, adapter.transport)


@pytest.mark.asyncio
async def test_eof_releasing_root_mutant_fails_fixture_check(
    adapters: OpenAIOfflineFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = OpenAISessions.retrieve

    async def release(self: OpenAISessions, scope: Scope, ref: ResourceRef) -> Session:
        value = await original(self, scope, ref)
        return value.model_copy(update={"state": "idle", "active_root_turn": None})

    monkeypatch.setattr(OpenAISessions, "retrieve", release)
    adapter = adapters()
    with pytest.raises(ConformanceFailure, match="child completion or EOF prematurely released"):
        await FIXTURES["C05"](adapter.driver, adapter.store, adapter.transport)


@pytest.mark.asyncio
async def test_unobserved_stop_mutant_fails_fixture_check(
    adapters: OpenAIOfflineFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stopped(
        self: OpenAIEvents, scope: Scope, receipt: CancelReceipt, *, deadline: datetime
    ) -> StopObservation:
        return StopObservation(
            receipt_operation_id=receipt.operation_id,
            stopped=True,
            outcome="interrupted",
            observed_at=datetime.now(UTC),
        )

    monkeypatch.setattr(OpenAIEvents, "wait_stopped", stopped)
    adapter = adapters()
    with pytest.raises(ConformanceFailure, match="EOF is not observed termination"):
        await FIXTURES["C06"](adapter.driver, adapter.store, adapter.transport)


@pytest.mark.asyncio
async def test_admission_bypass_mutant_fails_fixture_check(
    adapters: OpenAIOfflineFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = OpenAIDriver.admit

    def allow(self: OpenAIDriver, config: ConfigRevision) -> Admission:
        unchecked = ConfigRevision.create(
            config.channel,
            config.local,
            ResolvedBackend(backend=config.backend, profile=config.profile, model=config.model),
        )
        return original(self, unchecked)

    monkeypatch.setattr(OpenAIDriver, "admit", allow)
    adapter = adapters()
    with pytest.raises(ConformanceFailure, match="unsupported/unknown requirement admitted"):
        await FIXTURES["C10"](adapter.driver, adapter.store, adapter.transport)


@pytest.mark.asyncio
async def test_foreign_scope_mutant_fails_fixture_check(
    adapters: OpenAIOfflineFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = OpenAISessions.retrieve

    async def foreign(self: OpenAISessions, scope: Scope, ref: ResourceRef) -> Session:
        return await original(self, scope.model_copy(update={"tenant_id": "tenant"}), ref)

    monkeypatch.setattr(OpenAISessions, "retrieve", foreign)
    adapter = adapters()
    with pytest.raises(ConformanceFailure, match="foreign tenant reference admitted"):
        await FIXTURES["C10"](adapter.driver, adapter.store, adapter.transport)


@pytest.mark.asyncio
async def test_migration_success_mutant_fails_fixture_check(
    adapters: OpenAIOfflineFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def migrate(
        self: OpenAISessions,
        scope: Scope,
        ref: ResourceRef,
        target: ConfigRevision,
        *,
        expected: int,
        key: str,
    ) -> Session:
        return await self.retrieve(scope, ref)

    monkeypatch.setattr(OpenAISessions, "migrate", migrate)
    adapter = adapters()
    with pytest.raises(ConformanceFailure, match="migration unexpectedly supported"):
        await FIXTURES["C15"](adapter.driver, adapter.store, adapter.transport)


@pytest.mark.asyncio
async def test_raw_handle_mutant_fails_fixture_check(
    adapters: OpenAIOfflineFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = adapters()
    monkeypatch.setattr(adapter.driver, "client", object(), raising=False)
    with pytest.raises(ConformanceFailure, match="raw provider handle is public"):
        await FIXTURES["C16"](adapter.driver, adapter.store, adapter.transport)


@pytest.mark.asyncio
async def test_undeclared_extension_mutant_fails_fixture_check(
    adapters: OpenAIOfflineFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    def extension[T](self: OpenAIDriver, port: type[T], *, namespace: str, version: int) -> T:
        return cast(T, self.events)

    monkeypatch.setattr(OpenAIDriver, "extension", extension)
    adapter = adapters()
    with pytest.raises(ConformanceFailure, match="undeclared extension admitted"):
        await FIXTURES["C16"](adapter.driver, adapter.store, adapter.transport)


@pytest.mark.asyncio
async def test_undeclared_provider_error_is_fail_with_safe_evidence(
    adapters: OpenAIOfflineFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(self: EventNormalizer, item: Object):
        raise ProviderError("upstream", retryable=False, native_code="fictional-sensitive-detail")

    monkeypatch.setattr(EventNormalizer, "saved_item", fail)
    result = await run_fixture("C05", adapters())
    assert result.status == "fail" and result.pending_reason is None
    assert result.evidence == ("probe raised ProviderError",)


@pytest.mark.asyncio
async def test_missing_pending_declaration_cannot_silently_pass(
    adapters: OpenAIOfflineFactory,
) -> None:
    original = adapters()
    adapter = Adapter(
        driver=original.driver,
        store=original.store,
        transport=original.transport,
        pending={id_: reason for id_, reason in PENDING.items() if id_ != "C11"},
    )
    result = await run_fixture("C11", adapter)
    assert result.status == "fail" and result.pending_reason is None


@pytest.mark.asyncio
async def test_c04_wire_acceptance_survives_all_crashes_without_another_post(
    adapters: OpenAIOfflineFactory,
) -> None:
    adapter = adapters()
    result = await run_fixture("C04", adapter)
    assert result.status == "pass"
    wire = script(adapter)
    assert [send.key for send in wire.upstream_sends] == ["crash-0", "crash-2", "crash-3"]
    assert len(wire.accepted_inputs) == 3
    assert wire.mutation_count == 3
    assert wire.reconciled_sends == ()  # Saved text supplies no native key association.
    assert any(request.url.path.endswith("/items") for request in wire.requests)
    assert all(not send.receipt.input_ids for send in wire.upstream_sends)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", [False, True])
async def test_c04_lost_or_empty_ack_remains_unknown_after_restart(
    adapters: OpenAIOfflineFactory,
    lost: bool,
) -> None:
    adapter = adapters()
    wire = script(adapter)
    scenario = await wire.arrange("C04")
    if lost:
        wire.fault("timeout_after_accept")
    inputs = (UserMessage(content=(TextPart(text="identical native content"),)),)
    initial = await adapter.driver.events.send(
        scenario.scope, scenario.session.ref, inputs, key="uncertain"
    )
    assert initial.status == ("outcome_unknown" if lost else "queued")
    assert adapter.store is not None
    reopened = wire.restart_store(adapter.store)
    before = wire.mutation_count
    # Unrelated saved input with exactly the same text cannot prove correlation.
    wire.accepted_inputs.append({**wire.accepted_inputs[0], "id": "unrelated-identical-input"})
    recovered = await adapter.driver.events.send(
        scenario.scope, scenario.session.ref, inputs, key="uncertain"
    )
    assert recovered.status == "outcome_unknown" and recovered.input_ids == ()
    assert recovered.operation_id == initial.operation_id
    record = await reopened.get_operation(scenario.scope, "uncertain")
    assert record is not None
    assert record.operation.status == ("outcome_unknown" if lost else "accepted")
    assert wire.mutation_count == before == 1
    assert len(wire.upstream_sends) == 1


@pytest.mark.asyncio
async def test_c04_recovery_invented_input_identity_mutant_is_killed(
    adapters: OpenAIOfflineFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = OpenAIConformanceEvents.send

    async def invent(
        self: OpenAIConformanceEvents,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None = None,
    ) -> SendReceipt:
        result = await original(self, scope, session, events, key=key, expected_turn=expected_turn)
        if result.status == "outcome_unknown":
            return SendReceipt(
                operation_id=result.operation_id, status="processed", input_ids=(key,)
            )
        return result

    monkeypatch.setattr(OpenAIConformanceEvents, "send", invent)
    adapter = adapters()
    with pytest.raises(ConformanceFailure, match="ambiguous intent must stay unknown"):
        await FIXTURES["C04"](adapter.driver, adapter.store, adapter.transport)


@pytest.mark.asyncio
async def test_c04_blind_resend_mutant_is_killed(
    adapters: OpenAIOfflineFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = OpenAIConformanceEvents.send

    async def resend(
        self: OpenAIConformanceEvents,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None = None,
    ) -> SendReceipt:
        result = await original(self, scope, session, events, key=key, expected_turn=expected_turn)
        if result.status == "outcome_unknown":
            await OpenAIEvents.send(self, scope, session, events, key=key)
        return result

    monkeypatch.setattr(OpenAIConformanceEvents, "send", resend)
    adapter = adapters()
    with pytest.raises(ConformanceFailure, match="ambiguous/accepted recovery resent input"):
        await FIXTURES["C04"](adapter.driver, adapter.store, adapter.transport)
