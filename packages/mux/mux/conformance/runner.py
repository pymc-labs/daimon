"""Offline contract probes. Passing the reference is never provider certification."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.resources import Session, SessionSpec, SkillUpload


class ConformanceFailure(Exception):
    """A fixture-authored constant diagnostic, safe to include as evidence."""


def require(condition: object, message: str) -> None:
    """An optimization-safe check. Messages must be fixture-authored constants."""
    if not condition:
        raise ConformanceFailure(message)


class StateStore(Protocol):
    """Temporary opaque seam; replaced by mux.state.StateStore when N2 lands.

    No store operations are invented here. Every probe requiring them is pending.
    """


@dataclass(frozen=True)
class Scenario:
    scope: Scope
    foreign_scope: Scope
    session: Session
    desired: SessionSpec
    shared_resources: tuple[ResourceRef, ...] = ()


class ScriptedTransport(Protocol):
    """Driver-specific adapter seeds native responses, never returns a verdict.

    Each adapter must construct a fresh driver/store/transport per fixture.
    `arrange` seeds a populated session and the documented scenario faults.
    `fault` changes upstream responses without editing driver projections.
    `mutation_count` counts upstream writes (not reads or fixture seeding).
    `deleted_resources` records actual upstream deletes, independently of receipts.
    `skill_uploads` records decoded inline bundles actually received upstream,
    independently of driver inputs and returned skill metadata.
    """

    async def arrange(self, fixture_id: str) -> Scenario: ...
    def fault(self, name: str) -> None: ...
    @property
    def mutation_count(self) -> int: ...
    @property
    def deleted_resources(self) -> tuple[ResourceRef, ...]: ...
    @property
    def skill_uploads(self) -> tuple[SkillUpload, ...]: ...


@dataclass(frozen=True)
class Result:
    fixture_id: str
    status: Literal["pass", "fail", "pending"]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Adapter:
    driver: ManagedAgents
    store: StateStore | None
    transport: ScriptedTransport


Factory = Callable[[], Adapter]


class Registry:
    """Explicit registration prevents accidental discovery of real transports."""

    def __init__(self) -> None:
        self._factories: dict[str, Factory] = {}

    def register(self, name: str, factory: Factory) -> None:
        if name in self._factories:
            raise ValueError(f"adapter {name!r} already registered")
        self._factories[name] = factory

    def create(self, name: str) -> Adapter:
        return self._factories[name]()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._factories)


async def run(registry: Registry, name: str) -> tuple[Result, ...]:
    """Isolate every probe; a pending result prevents an all-pass certificate."""
    from mux.conformance.fixtures import FIXTURES

    results: list[Result] = []
    for fixture_id, probe in FIXTURES.items():
        adapter = registry.create(name)
        try:
            result = await probe(adapter.driver, adapter.store, adapter.transport)
        except ConformanceFailure as exc:
            result = Result(fixture_id, "fail", (f"check failed: {exc}",))
        except Exception as exc:
            # Do not serialize provider exception text, which can contain credentials.
            result = Result(fixture_id, "fail", (f"probe raised {type(exc).__name__}",))
        results.append(result)
    return tuple(results)
