"""Pinned C01–C18 outcomes and broken-driver checks; no live certification."""

import pytest
from mux.conformance.fixtures import FIXTURES
from mux.conformance.gemini import PENDING_REASONS, GeminiScript, adapter, register
from mux.conformance.runner import ConformanceFailure, PendingKind, Registry, run, run_fixture
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.usage import UsageObservation
from mux.drivers.gemini import GeminiManagedAgents, GeminiUsage


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["C03", "C06", "C07", "C13", "C15", "C16"])
async def test_executable_probe_uses_real_driver_and_store(fixture: str) -> None:
    script = GeminiScript()
    result = await FIXTURES[fixture](script.ma, script.store, script)
    assert result.status == "pass"
    assert result.evidence
    assert not script.ma.capabilities().core


EXPECTED_DEFERRED = {
    "C02": (
        PendingKind.ADAPTER_DEPENDENCY,
        "Environment snapshot resource port is scheduled for PR3.",
    ),
    "C04": (
        PendingKind.ADAPTER_DEPENDENCY,
        "Host fenced journal/cursor bridge is absent; send crash tests alone are insufficient.",
    ),
    "C05": (
        PendingKind.ADAPTER_DEPENDENCY,
        "No durable saved-item/SSE gap bridge; preview and EOF tests do not prove this fixture.",
    ),
    "C08": (
        PendingKind.CAPABILITY_UNAVAILABLE,
        "Inline reuse exposes no implemented in-place mount mutation transaction.",
    ),
    "C09": (
        PendingKind.CAPABILITY_UNAVAILABLE,
        "PR3 snapshot downloads are absent; shared provider vault/delete is unsupported.",
    ),
    "C10": (
        PendingKind.ADAPTER_DEPENDENCY,
        "Static Gemini support declarations cannot be changed by native admission faults.",
    ),
    "C11": (
        PendingKind.CAPABILITY_UNAVAILABLE,
        "Inline skills mount on an interaction, not a standalone provider skill upload API.",
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
        "C06",
        "C07",
        "C13",
        "C15",
        "C16",
    }
    assert {r.fixture_id for r in results if r.status == "pending"} == {
        "C01",
        "C02",
        "C04",
        "C05",
        "C08",
        "C09",
        "C10",
        "C11",
        "C12",
        "C14",
        "C17",
        "C18",
    }
    assert {r.fixture_id for r in results if r.pending_reason is not None} == set(EXPECTED_DEFERRED)


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
