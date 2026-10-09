"""Harness regressions live here so pytest-mux collects owned lane files."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

import pytest

from mux.conformance.fixtures import FIXTURES
from mux.conformance.reference import (
    ReferenceAgents,
    ReferenceArtifacts,
    ReferenceDriver,
    ReferenceEnvironments,
    ReferenceEvents,
    ReferenceSessions,
    ReferenceSkills,
    Transport,
    create,
)
from mux.conformance.runner import Adapter, Registry, Scenario, run
from mux.contracts.events import Event
from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.profile import Profile
from mux.contracts.receipts import CancelReceipt, DeletionReceipt, StopObservation
from mux.contracts.resources import Agent, Environment, Session, Skill, SkillUpload


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
    assert {r.fixture_id for r in results if r.status == "pending"} == {
        "C01",
        "C12",
        "C14",
        "C17",
        "C18",
    }
    assert {r.fixture_id for r in results if r.status == "pass"} == {
        "C02",
        "C03",
        "C04",
        "C07",
        "C13",
        "C05",
        "C06",
        "C08",
        "C09",
        "C10",
        "C11",
        "C15",
        "C16",
    }
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
        return Adapter(cast(ManagedAgents, driver), t.store, t)

    registry = Registry()
    registry.register("broken", broken)
    results = await run(registry, "broken")
    result = next(r for r in results if r.fixture_id == "C02")
    assert result.status == "fail" and result.evidence == (
        "check failed: loss must not silently start a fresh workspace",
    )


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


async def test_c05_rejects_duplicate_tool_result_with_new_journal_id() -> None:
    class DuplicateTools(ReferenceEvents):
        async def list(
            self, scope: Scope, session: ResourceRef, *, page: PageRequest
        ) -> Page[Event]:
            result = await super().list(scope, session, page=page)
            tools = [e for e in result.data if e.type == "agent.tool_result"]
            if tools:
                duplicate = tools[0].model_copy(update={"id": "duplicate-tool", "sequence": 99})
                return result.model_copy(update={"data": (*result.data, duplicate)})
            return result

    registry = Registry()
    registry.register("duplicate-tools", lambda: variant(events=DuplicateTools))
    results = await run(registry, "duplicate-tools")
    assert next(r for r in results if r.fixture_id == "C05").status == "fail"


@pytest.mark.parametrize("corrupt", [False, True])
async def test_c05_requires_preserved_message_and_tool_content(corrupt: bool) -> None:
    class MissingContent(ReferenceEvents):
        async def list(
            self, scope: Scope, session: ResourceRef, *, page: PageRequest
        ) -> Page[Event]:
            result = await super().list(scope, session, page=page)
            target = "agent.tool_result" if corrupt else "agent.message"
            data = tuple(
                e.model_copy(
                    update={
                        "payload": {
                            **e.payload,
                            "content": [{"type": "text", "text": "lost content"}],
                        }
                    }
                )
                if e.type == target
                else e
                for e in result.data
            )
            return result.model_copy(update={"data": data})

    registry = Registry()
    registry.register("missing-content", lambda: variant(events=MissingContent))
    results = await run(registry, "missing-content")
    assert next(r for r in results if r.fixture_id == "C05").status == "fail"


@pytest.mark.parametrize(
    "body,passed",
    [
        (bytes(range(256)), True),
        (bytes(range(255)), False),
        (bytes(range(256)) * 2, False),
        (b"corrupt", False),
    ],
)
async def test_c09_validates_successful_resume_bytes(body: bytes, passed: bool) -> None:
    class Resumable(ReferenceArtifacts):
        async def download(self, scope: Scope, ref: ResourceRef) -> AsyncIterator[bytes]:
            if "download_interrupted" in self.t.faults:
                yield body
            else:
                async for chunk in super().download(scope, ref):
                    yield chunk

    registry = Registry()
    registry.register("resumable", lambda: variant(artifacts=Resumable))
    results = await run(registry, "resumable")
    assert (next(r for r in results if r.fixture_id == "C09").status == "pass") is passed


async def test_c09_accepts_typed_explicit_download_failure() -> None:
    a = create()
    result = await FIXTURES["C09"](a.driver, a.store, a.transport)
    assert result.status == "pass"
    assert isinstance(a.transport, Transport) and "download_interrupted" in a.transport.faults


async def test_c06_works_with_a_deadline_honoring_waiter() -> None:
    deadlines: list[datetime] = []

    class DeadlineAware(ReferenceEvents):
        async def wait_stopped(
            self, scope: Scope, receipt: CancelReceipt, *, deadline: datetime
        ) -> StopObservation:
            deadlines.append(deadline)
            if datetime.now(UTC) >= deadline:
                return StopObservation(
                    receipt_operation_id=receipt.operation_id,
                    stopped=False,
                    observed_at=datetime.now(UTC),
                )
            return await super().wait_stopped(scope, receipt, deadline=deadline)

    a = variant(events=DeadlineAware)
    result = await FIXTURES["C06"](a.driver, a.store, a.transport)
    assert result.status == "pass" and len(deadlines) == 2
    assert all(deadline > datetime.now(UTC) for deadline in deadlines)


def variant(
    *,
    events: type[ReferenceEvents] = ReferenceEvents,
    artifacts: type[ReferenceArtifacts] = ReferenceArtifacts,
) -> Adapter:
    t = Transport()
    driver = ReferenceDriver(t)
    driver.events = events(t)
    driver.artifacts = artifacts(t)
    return Adapter(cast(ManagedAgents, driver), t.store, t)


async def test_c05_child_completion_does_not_release_root_before_reconcile() -> None:
    class ChildEndsRoot(ReferenceEvents):
        async def stream(
            self,
            scope: Scope,
            session: ResourceRef,
            *,
            after: str | None = None,
            previews: bool = False,
        ) -> AsyncIterator[Event]:
            async for event in super().stream(scope, session, after=after, previews=previews):
                yield event
            if self.t.fixture == "C05":
                self.t.session = self.t.session.model_copy(
                    update={"state": "idle", "active_root_turn": None}
                )

    registry = Registry()
    registry.register("child-ends-root", lambda: variant(events=ChildEndsRoot))
    results = await run(registry, "child-ends-root")
    failure = next(r for r in results if r.fixture_id == "C05")
    assert failure.status == "fail"
    assert failure.evidence == (
        "check failed: child completion or EOF prematurely released the root turn",
    )


def test_optimized_python_preserves_checks_and_reports_diagnostic() -> None:
    code = """
import asyncio
import sys
import subprocess
from typing import cast
from mux.conformance.reference import ReferenceDriver, ReferenceEvents, Transport
from mux.conformance.runner import Adapter, Registry, run
from mux.contracts.ports import ManagedAgents
class Broken(ReferenceEvents):
    async def wait_stopped(self, scope, receipt, *, deadline):
        stopped = await super().wait_stopped(scope, receipt, deadline=deadline)
        return stopped.model_copy(update={"stopped": True, "outcome": "interrupted"})
def factory():
    t = Transport()
    driver = ReferenceDriver(t)
    driver.events = Broken(t)
    return Adapter(cast(ManagedAgents, driver), t.store, t)
async def main():
    registry = Registry()
    registry.register("broken-reference", factory)
    result = next(r for r in await run(registry, "broken-reference") if r.fixture_id == "C06")
    if result.status != "fail" or "EOF is not observed termination" not in result.evidence[0]:
        raise RuntimeError("optimized Python silently removed conformance checks")
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", code], text=True, capture_output=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr


async def test_c09_detects_provider_deletion_hidden_by_a_retained_receipt() -> None:
    class LyingDelete(ReferenceSessions):
        async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
            receipt = await super().delete(scope, ref, key=key)
            self.t.deletions.extend(receipt.retained)
            return receipt

    t = Transport()
    driver = ReferenceDriver(t)
    driver.sessions = LyingDelete(t)
    registry = Registry()
    registry.register("lying-delete", lambda: Adapter(cast(ManagedAgents, driver), t.store, t))
    result = next(r for r in await run(registry, "lying-delete") if r.fixture_id == "C09")
    assert result.status == "fail"
    assert "actually delete shared resources" in result.evidence[0]


async def test_c10_no_extension_profile_records_unexercised_version_check() -> None:
    class NoExtensions(ReferenceDriver):
        def capabilities(self) -> Profile:
            return super().capabilities().model_copy(update={"extensions": ()})

    t = Transport()
    result = await FIXTURES["C10"](cast(ManagedAgents, NoExtensions(t)), None, t)
    assert result.status == "pass"
    assert "extension version check not applicable" in result.evidence[-1]


def test_fixture_checks_cannot_reintroduce_optimized_away_assertions() -> None:
    import ast
    import inspect

    from mux.conformance import fixtures, state_fixtures

    for module in (fixtures, state_fixtures):
        tree = ast.parse(inspect.getsource(module))
        assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree))


@pytest.mark.parametrize(
    "fault", ["missing_pin", "unbound_agent", "lost_record", "missing_repository"]
)
async def test_c11_rejects_resource_records_without_deployment_evidence(fault: str) -> None:
    class BrokenSkills(ReferenceSkills):
        reads = 0

        async def retrieve(self, scope: Scope, skill_id: str) -> Skill:
            skill = await super().retrieve(scope, skill_id)
            self.reads += 1
            if fault == "missing_pin" or (fault == "lost_record" and self.reads > 1):
                return skill.model_copy(update={"latest_version": None})
            return skill

    class BrokenAgents(ReferenceAgents):
        async def retrieve(self, scope: Scope, ref: ResourceRef) -> Agent:
            agent = await super().retrieve(scope, ref)
            if fault == "unbound_agent":
                return agent.model_copy(
                    update={"spec": agent.spec.model_copy(update={"skills": None})}
                )
            return agent

    class BrokenEnvironments(ReferenceEnvironments):
        async def retrieve(self, scope: Scope, ref: ResourceRef) -> Environment:
            environment = await super().retrieve(scope, ref)
            if fault == "missing_repository":
                return environment.model_copy(
                    update={"spec": environment.spec.model_copy(update={"sources": None})}
                )
            return environment

    def broken() -> Adapter:
        t = Transport()
        driver = ReferenceDriver(t)
        driver.skills = BrokenSkills(t)
        driver.agents = BrokenAgents(t)
        driver.environments = BrokenEnvironments(t)
        return Adapter(cast(ManagedAgents, driver), t.store, t)

    registry = Registry()
    registry.register("broken-resource-reference", broken)
    result = next(
        r for r in await run(registry, "broken-resource-reference") if r.fixture_id == "C11"
    )
    assert result.status == "fail" and result.evidence[0].startswith("check failed: C11:")


@pytest.mark.parametrize("metadata", ["no_digest", "enrichment", "opaque_digest"])
async def test_c11_accepts_explicit_version_with_optional_metadata(metadata: str) -> None:
    class ValidSkills(ReferenceSkills):
        reads = 0

        async def retrieve(self, scope: Scope, skill_id: str) -> Skill:
            skill = await super().retrieve(scope, skill_id)
            self.reads += 1
            assert skill.latest_version is not None
            values = (
                {"digest": None, "source": None}
                if metadata == "no_digest" or (metadata == "enrichment" and self.reads == 1)
                else {"digest": "provider:opaque-bundle-digest", "source": "custom"}
            )
            return skill.model_copy(
                update={"latest_version": skill.latest_version.model_copy(update=values)}
            )

    class ValidAgents(ReferenceAgents):
        async def retrieve(self, scope: Scope, ref: ResourceRef) -> Agent:
            agent = await super().retrieve(scope, ref)
            assert agent.spec.skills is not None
            values = (
                {"digest": None, "source": None}
                if metadata == "no_digest"
                else {"digest": "provider:opaque-bundle-digest", "source": "custom"}
            )
            return agent.model_copy(
                update={
                    "spec": agent.spec.model_copy(
                        update={"skills": (agent.spec.skills[0].model_copy(update=values),)}
                    )
                }
            )

    t = Transport()
    driver = ReferenceDriver(t)
    driver.skills = ValidSkills(t)
    driver.agents = ValidAgents(t)
    result = await FIXTURES["C11"](cast(ManagedAgents, driver), None, t)
    assert result.status == "pass"


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_id",
        "wrong_version",
        "missing_version",
        "wrong_source",
        "wrong_digest",
        "conflicting_enriched_source",
        "conflicting_enriched_digest",
        "corrupt_upload",
        "missing_upload",
    ],
)
async def test_c11_rejects_conflicting_pins_and_invalid_uploaded_bytes(fault: str) -> None:
    class BrokenSkills(ReferenceSkills):
        reads = 0

        async def retrieve(self, scope: Scope, skill_id: str) -> Skill:
            skill = await super().retrieve(scope, skill_id)
            self.reads += 1
            assert skill.latest_version is not None
            if fault == "wrong_source":
                pin = skill.latest_version.model_copy(update={"source": "custom"})
            elif fault.startswith("conflicting_enriched_"):
                field = "source" if fault.endswith("source") else "digest"
                pin = skill.latest_version.model_copy(
                    update={field: None if self.reads == 1 else "conflicting-metadata"}
                )
            else:
                return skill
            return skill.model_copy(update={"latest_version": pin})

        async def create(self, scope: Scope, bundle: SkillUpload, *, key: str) -> Skill:
            skill = await super().create(scope, bundle, key=key)
            if fault == "corrupt_upload":
                self.t.uploads[-1] = bundle.model_copy(
                    update={
                        "files": (bundle.files[0].model_copy(update={"content": b"corrupted"}),)
                    }
                )
            elif fault == "missing_upload":
                self.t.uploads.pop()
            return skill

    class BrokenAgents(ReferenceAgents):
        async def retrieve(self, scope: Scope, ref: ResourceRef) -> Agent:
            agent = await super().retrieve(scope, ref)
            assert agent.spec.skills is not None
            changes: dict[str, dict[str, str | None]] = {
                "wrong_id": {"id": "other"},
                "wrong_version": {"version": "v0"},
                "missing_version": {"version": None},
                "wrong_source": {"source": "other"},
                "wrong_digest": {"digest": "other"},
                "conflicting_enriched_source": {"source": "custom"},
            }
            values = changes.get(fault, {})
            return agent.model_copy(
                update={
                    "spec": agent.spec.model_copy(
                        update={"skills": (agent.spec.skills[0].model_copy(update=values),)}
                    )
                }
            )

    def broken() -> Adapter:
        t = Transport()
        driver = ReferenceDriver(t)
        driver.skills = BrokenSkills(t)
        driver.agents = BrokenAgents(t)
        return Adapter(cast(ManagedAgents, driver), t.store, t)

    registry = Registry()
    registry.register("conflicting-skill-reference", broken)
    result = next(
        r for r in await run(registry, "conflicting-skill-reference") if r.fixture_id == "C11"
    )
    assert result.status == "fail" and result.evidence[0].startswith("check failed: C11:")
