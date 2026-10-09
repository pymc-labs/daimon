"""Explicit adapter gaps remain visible and never certify or hide other failures."""

from __future__ import annotations

import subprocess
import sys
from typing import cast

import pytest

from mux.conformance.reference import ReferenceDriver, ReferenceEvents, Transport, create
from mux.conformance.runner import (
    Adapter,
    PendingKind,
    PendingReason,
    Registry,
    Result,
    run,
    run_fixture,
)
from mux.contracts.errors import UnsupportedCapability
from mux.contracts.events import Event
from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope
from mux.contracts.ports import ManagedAgents


@pytest.mark.parametrize("kind", list(PendingKind))
async def test_declared_pending_defers_fixture_but_runs_other_probes(kind: PendingKind) -> None:
    a = create()
    reason = PendingReason(kind, "adapter-authored gap")
    declared = {"C02": reason}
    adapter = Adapter(a.driver, a.store, a.transport, pending=declared)
    declared.clear()  # The declaration is snapshotted, not mutable configuration.
    result = await run_fixture("C02", adapter)
    assert result.status == "pending" and result.pending_reason == reason
    assert result.evidence == (f"adapter pending ({kind.value}): adapter-authored gap",)
    assert cast(Transport, adapter.transport).fixture == ""  # No arrange or port calls.
    assert (await run_fixture("C16", adapter)).status == "pass"


async def test_registry_emits_all_ids_and_structured_pending_reasons() -> None:
    reason = PendingReason(PendingKind.CAPABILITY_UNAVAILABLE, "no native vault API")

    def factory() -> Adapter:
        a = create()
        return Adapter(a.driver, a.store, a.transport, pending={"C09": reason})

    registry = Registry()
    registry.register("fake-with-declared-gap", factory)
    results = await run(registry, "fake-with-declared-gap")
    assert len(results) == 18
    assert [r.fixture_id for r in results] == [f"C{i:02}" for i in range(1, 19)]
    assert results[8].status == "pending" and results[8].pending_reason == reason
    assert results[15].status == "pass"
    assert any(r.status == "pending" for r in results)  # An all-pass gate refuses it.


async def test_undeclared_unsupported_capability_still_fails() -> None:
    class MissingEvents(ReferenceEvents):
        async def list(
            self, scope: Scope, session: ResourceRef, *, page: PageRequest
        ) -> Page[Event]:
            raise UnsupportedCapability(("event_history",), "fake")

    t = Transport()
    driver = ReferenceDriver(t)
    driver.events = MissingEvents(t)
    reason = PendingReason(PendingKind.ADAPTER_DEPENDENCY, "C02 only")
    a = Adapter(cast(ManagedAgents, driver), t.store, t, pending={"C02": reason})
    result = await run_fixture("C16", a)
    assert result.status == "fail" and result.pending_reason is None
    assert result.evidence == ("probe raised UnsupportedCapability",)


def test_invalid_declarations_and_pass_with_pending_reason_are_refused() -> None:
    with pytest.raises(ValueError, match="typed kind"):
        PendingReason(cast(PendingKind, "unknown_kind"), "gap")
    with pytest.raises(ValueError, match="nonempty"):
        PendingReason(PendingKind.LIVE_KEY_REQUIRED, " ")
    a = create()
    reason = PendingReason(PendingKind.LIVE_KEY_REQUIRED, "no probe key")
    with pytest.raises(ValueError, match="valid fixture IDs"):
        Adapter(a.driver, a.store, a.transport, pending={"C19": reason})
    with pytest.raises(ValueError, match="typed reasons"):
        Adapter(a.driver, a.store, a.transport, pending={"C02": cast(PendingReason, "gap")})
    with pytest.raises(ValueError, match="cannot certify"):
        Result("C02", "pass", (), pending_reason=reason)


def test_pending_and_its_validation_survive_optimized_python() -> None:
    script = """
import asyncio
from mux.conformance.reference import create
from mux.conformance.runner import Adapter, PendingKind, PendingReason, run_fixture
a = create()
reason = PendingReason(PendingKind.LIVE_KEY_REQUIRED, "no probe key")
adapter = Adapter(a.driver, a.store, a.transport, pending={"C02": reason})
result = asyncio.run(run_fixture("C02", adapter))
if result.status != "pending" or result.pending_reason != reason:
    raise SystemExit("optimized pending changed")
try:
    PendingReason(PendingKind.LIVE_KEY_REQUIRED, "")
except ValueError:
    pass
else:
    raise SystemExit("optimized validation vanished")
"""
    checked = subprocess.run(
        [sys.executable, "-O", "-c", script], capture_output=True, text=True, timeout=30
    )
    assert checked.returncode == 0, checked.stderr
