"""Pinned C01–C18 outcomes and broken-driver checks; no live certification."""

import subprocess
import sys
from collections.abc import Sequence

import pytest
from mux.conformance.fixtures import FIXTURES
from mux.conformance.gemini import PENDING_REASONS, GeminiScript, adapter, register
from mux.conformance.runner import (
    Adapter,
    ConformanceFailure,
    PendingKind,
    Registry,
    Scenario,
    require,
    run,
    run_fixture,
)
from mux.contracts.actions import InputEvent
from mux.contracts.admission import Admission
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import PageRequest, ResourceRef, Scope
from mux.contracts.profile import Capability
from mux.contracts.receipts import SendReceipt
from mux.contracts.resources import ProjectionSnapshot, Session
from mux.contracts.usage import UsageObservation
from mux.drivers.gemini import GeminiManagedAgents, GeminiUsage
from mux.drivers.gemini.core import GeminiEvents, GeminiSessions
from mux.profiles.gemini import INLINE_REUSE


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["C03", "C05", "C06", "C07", "C10", "C13", "C15", "C16"])
async def test_executable_probe_uses_real_driver_and_store(fixture: str) -> None:
    script = GeminiScript()
    result = await FIXTURES[fixture](script.ma, script.store, script)
    assert result.status == "pass"
    assert result.evidence
    assert not script.ma.capabilities().core


EXPECTED_DEFERRED = {
    "C01": (
        PendingKind.ADAPTER_DEPENDENCY,
        "Multi-human admission/batching host scenario and conformance hook are absent.",
    ),
    "C02": (
        PendingKind.ADAPTER_DEPENDENCY,
        "Snapshot bytes implemented; clock-driven expiry and unexpected-loss proof absent.",
    ),
    "C04": (
        PendingKind.ADAPTER_DEPENDENCY,
        "Ambiguous accepted POST reconciliation is absent; provider offers no idempotency lookup.",
    ),
    "C08": (
        PendingKind.ADAPTER_DEPENDENCY,
        "Driver next-turn tool/mount update port is not implemented.",
    ),
    "C09": (
        PendingKind.CAPABILITY_UNAVAILABLE,
        "Binary snapshots are proved; provider vault API and session-delete receipt are unavailable.",
    ),
    "C11": (
        PendingKind.ADAPTER_DEPENDENCY,
        "Conformance bridge for interaction-time inline skill deployment is absent.",
    ),
    "C12": (
        PendingKind.ADAPTER_DEPENDENCY,
        "Gemini host ledger/outbox recovery scenario has not been connected to the accounting hook.",
    ),
    "C14": (
        PendingKind.ADAPTER_DEPENDENCY,
        "Existing/new-thread backend selection host scenario and conformance hook are absent.",
    ),
    "C17": (
        PendingKind.ADAPTER_DEPENDENCY,
        "Host wake generation/lease fencing scenario and conformance hook are absent.",
    ),
    "C18": (
        PendingKind.ADAPTER_DEPENDENCY,
        "Gemini host termination outcome persistence scenario and conformance hook are absent.",
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", tuple(EXPECTED_DEFERRED))
async def test_declared_pending_kind_and_reason_are_pinned(fixture: str) -> None:
    item = adapter()
    result = await run_fixture(fixture, item)
    assert result.status == "pending" and result.pending_reason is not None
    assert (result.pending_reason.kind, result.pending_reason.detail) == EXPECTED_DEFERRED[fixture]
    assert item.transport.mutation_count == 0
    assert result.pending_reason == PENDING_REASONS[fixture]


@pytest.mark.asyncio
async def test_registry_matrix_cannot_certify_deferred_scenarios() -> None:
    registry = Registry()
    register(registry)
    results = await run(registry, "gemini.inline_reuse.offline")
    assert len(results) == 18 and not any(r.status == "fail" for r in results)
    assert {r.fixture_id for r in results if r.status == "pass"} == {
        "C03",
        "C05",
        "C06",
        "C07",
        "C10",
        "C13",
        "C15",
        "C16",
    }
    assert {r.fixture_id for r in results if r.status == "pending"} == {
        "C01",
        "C02",
        "C04",
        "C08",
        "C09",
        "C11",
        "C12",
        "C14",
        "C17",
        "C18",
    }
    assert {r.fixture_id for r in results if r.pending_reason is not None} == set(EXPECTED_DEFERRED)


def test_unavailable_reasons_match_unsupported_profile_capabilities() -> None:
    unavailable: dict[str, Capability] = {"C09": "vaults"}
    assert {
        fixture
        for fixture, reason in PENDING_REASONS.items()
        if reason.kind == PendingKind.CAPABILITY_UNAVAILABLE
    } == set(unavailable)
    for capability in unavailable.values():
        assert INLINE_REUSE.support_for(capability) == "unsupported"


ORIGINAL_SEND = GeminiEvents.send
ORIGINAL_RETRIEVE = GeminiSessions.retrieve
ORIGINAL_RECONCILE = GeminiEvents.reconcile


async def reconcile_hides_gap(
    self: GeminiEvents, scope: Scope, session: ResourceRef
) -> ProjectionSnapshot:
    projection = await ORIGINAL_RECONCILE(self, scope, session)
    return projection.model_copy(update={"gaps": ()})


async def timeout_reported_as_queued(
    self: GeminiEvents,
    scope: Scope,
    session: ResourceRef,
    events: Sequence[InputEvent],
    *,
    key: str,
    expected_turn: str | None = None,
) -> SendReceipt:
    receipt = await ORIGINAL_SEND(
        self, scope, session, events, key=key, expected_turn=expected_turn
    )
    return (
        receipt.model_copy(update={"status": "queued"})
        if receipt.status == "outcome_unknown"
        else receipt
    )


async def retrieve_releases_occupancy(
    self: GeminiSessions, scope: Scope, ref: ResourceRef
) -> Session:
    session = await ORIGINAL_RETRIEVE(self, scope, ref)
    return session.model_copy(update={"state": "idle", "active_root_turn": None})


def admit_unconditionally(self: GeminiManagedAgents, config: ConfigRevision) -> Admission:
    return Admission(
        provider=config.backend,
        profile_id=config.profile,
        model=config.model,
        thread_mode=config.thread_mode,
        config_local=config.local,
        config_digest=config.digest,
        satisfied=tuple(config.requires),
    )


async def migration_supported(
    self: GeminiSessions,
    scope: Scope,
    ref: ResourceRef,
    target: ConfigRevision,
    *,
    expected: int,
    key: str,
) -> Session:
    return await ORIGINAL_RETRIEVE(self, scope, ref)


def install_driver_mutant(monkeypatch: pytest.MonkeyPatch, fixture: str) -> None:
    # Patch the real port classes so C03's driver restart retains the mutation.
    if fixture == "C05":
        monkeypatch.setattr(GeminiEvents, "reconcile", reconcile_hides_gap)
    elif fixture == "C03":
        monkeypatch.setattr(GeminiEvents, "send", timeout_reported_as_queued)
    elif fixture == "C06":
        monkeypatch.setattr(GeminiSessions, "retrieve", retrieve_releases_occupancy)
    elif fixture == "C10":
        monkeypatch.setattr(GeminiManagedAgents, "admit", admit_unconditionally)
    elif fixture == "C15":
        monkeypatch.setattr(GeminiSessions, "migrate", migration_supported)
    else:
        raise ValueError("unknown driver mutant")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture", "failure"),
    [
        ("C03", "acceptance timeout must stay unknown"),
        ("C05", "unrecoverable history loss must have an explicit gap"),
        ("C06", "cancel request without stop must hold root occupancy"),
        ("C10", "unsupported/unknown requirement admitted"),
        ("C15", "migration unexpectedly supported"),
    ],
)
async def test_driver_mutant_is_rejected(
    monkeypatch: pytest.MonkeyPatch, fixture: str, failure: str
) -> None:
    install_driver_mutant(monkeypatch, fixture)
    script = GeminiScript()
    with pytest.raises(ConformanceFailure, match=failure):
        await FIXTURES[fixture](script.ma, script.store, script)


def ignore_fault(self: GeminiScript, name: str) -> None:
    pass


@pytest.mark.asyncio
async def test_ignored_transport_fault_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GeminiScript, "fault", ignore_fault)
    script = GeminiScript()
    with pytest.raises(ConformanceFailure, match="interrupted outcome must be observed"):
        await FIXTURES["C06"](script.ma, script.store, script)


class LeakingDriver(GeminiManagedAgents):
    raw_client = object()


@pytest.mark.asyncio
async def test_raw_handle_mutant_fails_c16() -> None:
    script = GeminiScript()
    script.ma = LeakingDriver(
        script, storage=script.storage, state_store=script.store, account_scope_id="project"
    )
    with pytest.raises(ConformanceFailure, match="raw provider handle"):
        await FIXTURES["C16"](script.ma, script.store, script)


class ZeroingUsage(GeminiUsage):
    async def reconcile(self, scope: Scope, session: ResourceRef) -> tuple[UsageObservation, ...]:
        observations = await super().reconcile(scope, session)
        return tuple(
            o.model_copy(update={"output_tokens": 0}) if o.output_tokens is None else o
            for o in observations
        )


@pytest.mark.asyncio
async def test_null_is_zero_mutant_fails_c07() -> None:
    script = GeminiScript()
    script.ma.usage = ZeroingUsage(script, script.storage, "project")
    with pytest.raises(ConformanceFailure, match="null and the full correction"):
        await FIXTURES["C07"](script.ma, script.store, script)


async def verify_optimized_conformance() -> None:
    registry = Registry()
    register(registry)
    results = await run(registry, "gemini.inline_reuse.offline")
    require(len(results) == 18, "optimized matrix must report all fixtures")
    require(
        {r.fixture_id for r in results if r.status == "pass"}
        == {"C03", "C05", "C06", "C07", "C10", "C13", "C15", "C16"},
        "optimized matrix must retain all eight executable probes",
    )
    require(
        {r.fixture_id for r in results if r.status == "pending"}
        == {"C01", "C02", "C04", "C08", "C09", "C11", "C12", "C14", "C17", "C18"},
        "optimized matrix must retain ten declared/host pending results",
    )
    for result in results:
        if result.fixture_id in EXPECTED_DEFERRED:
            reason = result.pending_reason
            require(
                reason is not None
                and (reason.kind, reason.detail) == EXPECTED_DEFERRED[result.fixture_id],
                "optimized pending reason changed",
            )
    for fixture in ("C03", "C05", "C06", "C07", "C10", "C15", "C16", "fake_fault"):
        with pytest.MonkeyPatch.context() as monkeypatch:
            if fixture in ("C03", "C05", "C06", "C10", "C15"):
                install_driver_mutant(monkeypatch, fixture)
            elif fixture == "fake_fault":
                monkeypatch.setattr(GeminiScript, "fault", ignore_fault)
            script = GeminiScript()
            if fixture == "C07":
                script.ma.usage = ZeroingUsage(script, script.storage, "project")
            elif fixture == "C16":
                script.ma = LeakingDriver(
                    script,
                    storage=script.storage,
                    state_store=script.store,
                    account_scope_id="project",
                )
            result = await run_fixture(
                "C06" if fixture == "fake_fault" else fixture,
                Adapter(script.ma, script.store, script),
            )
            require(result.status == "fail", f"optimized mutant survived: {fixture}")


def test_optimized_matrix_and_all_mutants() -> None:
    subprocess.run(
        [
            sys.executable,
            "-O",
            "-c",
            "import asyncio; from packages.mux.tests.drivers.gemini.test_conformance "
            "import verify_optimized_conformance; asyncio.run(verify_optimized_conformance())",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_c05_gap_survives_restart_without_preview_or_child_end_in_history() -> None:
    script = GeminiScript()
    scenario = await script.arrange("C05")
    streamed = [
        e
        async for e in script.ma.events.stream(scenario.scope, scenario.session.ref, previews=True)
    ]
    assert script.stream_closed
    assert any(e.authority == "preview" for e in streamed)
    assert not any(e.type == "session.turn_ended" for e in streamed)
    assert len([e for e in streamed if e.type == "session.history_gap"]) == 1
    script.restart_store(script.store)
    before = await script.ma.events.list(scenario.scope, scenario.session.ref, page=PageRequest())
    gaps = [e for e in before.data if e.type == "session.history_gap"]
    assert len(gaps) == 1 and gaps[0].payload["recoverable"] is False
    assert gaps[0].native is not None and gaps[0].native.provider == "gemini"
    assert not any(e.authority == "preview" or e.type == "agent.message" for e in before.data)
    running = await script.ma.events.reconcile(scenario.scope, scenario.session.ref)
    assert running.state == "running" and running.active_root_turn == "root"
    assert running.gaps == (gaps[0].id,)
    completed = await script.ma.events.reconcile(scenario.scope, scenario.session.ref)
    assert completed.state == "idle" and completed.active_root_turn is None
    assert completed.gaps == running.gaps
    after = await script.ma.events.list(scenario.scope, scenario.session.ref, page=PageRequest())
    assert len([e for e in after.data if e.type == "session.turn_ended"]) == 1
    assert len({e.id for e in after.data}) == len(after.data)
    assert script.read_requests and set(script.read_requests) == {"root"}
    steps = script.saved["root"]["steps"]
    assert isinstance(steps, list) and all(
        isinstance(step, dict) and "id" not in step for step in steps
    )


@pytest.mark.asyncio
async def test_c05_default_alias_is_preserved_and_wrong_native_alias_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    original = GeminiScript.arrange

    async def wrong_alias(self: GeminiScript, fixture_id: str) -> Scenario:
        return replace(await original(self, fixture_id), saved_message_item_id="item")

    monkeypatch.setattr(GeminiScript, "arrange", wrong_alias)
    script = GeminiScript()
    with pytest.raises(ConformanceFailure, match="saved message identity or text was altered"):
        await FIXTURES["C05"](script.ma, script.store, script)
