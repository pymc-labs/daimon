"""Harness regressions live here so pytest-mux collects owned lane files."""

from __future__ import annotations

from typing import cast

import pytest

from mux.conformance.fixtures import FIXTURES
from mux.conformance.reference import ReferenceDriver, ReferenceSessions, Transport, create
from mux.conformance.runner import Adapter, Registry, Scenario, run
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.resources import Session


@pytest.mark.parametrize("fixture_id", FIXTURES)
async def test_reference_matrix(fixture_id: str) -> None:
    a = create()
    result = await FIXTURES[fixture_id](a.driver, a.store, a.transport)
    if result.status == "pending":
        pytest.skip(result.evidence[0])
    assert result.status == "pass" and result.evidence


async def test_pending_is_never_success_and_fixtures_are_isolated() -> None:
    registry = Registry()
    count = 0

    def factory() -> Adapter:
        nonlocal count
        count += 1
        return create()

    registry.register("reference", factory)
    results = await run(registry, "reference")
    assert count == 18
    assert [r.fixture_id for r in results] == [f"C{i:02}" for i in range(1, 19)]
    assert {r.status for r in results} == {"pass", "pending"}
    assert all(r.evidence for r in results)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("reference", factory)


async def test_runner_detects_silent_workspace_reset() -> None:
    class BrokenSessions(ReferenceSessions):
        async def retrieve(self, scope: Scope, ref: ResourceRef) -> Session:
            return self.t.session

    def broken() -> Adapter:
        t = Transport()
        driver = ReferenceDriver(t)
        driver.sessions = BrokenSessions(t)
        return Adapter(cast(ManagedAgents, driver), None, t)

    registry = Registry()
    registry.register("broken", broken)
    results = await run(registry, "broken")
    result = next(r for r in results if r.fixture_id == "C02")
    assert result.status == "fail" and result.evidence == ("probe raised AssertionError",)


async def test_runner_does_not_export_exception_text() -> None:
    class BrokenTransport(Transport):
        async def arrange(self, fixture_id: str) -> Scenario:
            raise RuntimeError("sensitive upstream value")

    def broken() -> Adapter:
        a = create()
        return Adapter(a.driver, None, BrokenTransport())

    registry = Registry()
    registry.register("broken", broken)
    results = await run(registry, "broken")
    assert any(r.status == "fail" for r in results)
    assert all("sensitive" not in str(r.evidence) for r in results)
