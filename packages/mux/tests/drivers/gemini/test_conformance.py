"""Executable draft probes and broken-driver checks; no certification claim."""

import pytest
from mux.conformance.fixtures import FIXTURES
from mux.conformance.gemini import PENDING_REASONS, GeminiScript, PendingScenario, register
from mux.conformance.runner import ConformanceFailure, Registry, run
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.usage import UsageObservation
from mux.drivers.gemini import GeminiManagedAgents, GeminiUsage


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["C03", "C06", "C07", "C13", "C15", "C16"])
async def test_executable_draft_probe_uses_real_driver_and_store(fixture: str) -> None:
    script = GeminiScript()
    result = await FIXTURES[fixture](script.ma, script.store, script)
    assert result.status == "pass"
    assert result.evidence
    assert not script.ma.capabilities().core


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", tuple(PENDING_REASONS))
async def test_unimplemented_scenarios_have_explicit_draft_reasons(fixture: str) -> None:
    script = GeminiScript()
    with pytest.raises(PendingScenario) as caught:
        await script.arrange(fixture)
    assert str(caught.value) == PENDING_REASONS[fixture]
    assert script.mutation_count == 0


@pytest.mark.asyncio
async def test_current_runner_cannot_certify_the_draft_pending_matrix() -> None:
    registry = Registry()
    register(registry)
    results = await run(registry, "gemini.inline_reuse.offline")
    # N9 owns typed PENDING support. Until it lands these safe exceptions FAIL.
    assert {r.fixture_id for r in results if r.status == "fail"} == set(PENDING_REASONS)
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
        "C12",
        "C14",
        "C17",
        "C18",
    }


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
