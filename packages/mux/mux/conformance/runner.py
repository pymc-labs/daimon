"""Offline contract probes. Passing the reference is never provider certification."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, Protocol

from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import SendReceipt
from mux.contracts.resources import Session, SessionSpec, SkillUpload
from mux.state.memory import CrashPoint
from mux.state.store import StateStore


class ConformanceFailure(Exception):
    """A fixture-authored constant diagnostic, safe to include as evidence."""


def require(condition: object, message: str) -> None:
    """An optimization-safe check. Messages must be fixture-authored constants."""
    if not condition:
        raise ConformanceFailure(message)


@dataclass(frozen=True)
class Scenario:
    scope: Scope
    foreign_scope: Scope
    session: Session
    desired: SessionSpec
    shared_resources: tuple[ResourceRef, ...] = ()
    saved_message_item_id: str = "item"


@dataclass(frozen=True)
class SendEvidence:
    key: str
    receipt: SendReceipt


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
    @property
    def upstream_sends(self) -> tuple[SendEvidence, ...]:
        """Actual scripted acceptance responses, independent of local records."""
        ...

    @property
    def reconciled_sends(self) -> tuple[SendEvidence, ...]:
        """Native responses actually read by recovery, never invented from the store."""
        ...

    def restart_store(
        self, store: StateStore, *, crash: Mapping[str, CrashPoint] | None = None
    ) -> StateStore:
        """Restart over committed data and reattach the driver to that store.

        Crash plans simulate death immediately before/after a named store
        transaction commits. Upstream effects survive independently.
        """
        ...


class PendingKind(StrEnum):
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    LIVE_KEY_REQUIRED = "live_key_required"
    ADAPTER_DEPENDENCY = "adapter_dependency"


@dataclass(frozen=True)
class PendingReason:
    """Adapter-authored explanation, never a native exception message."""

    kind: PendingKind
    detail: str

    def __post_init__(self) -> None:
        if type(self.kind) is not PendingKind or not self.detail.strip():
            raise ValueError("pending declarations require a typed kind and nonempty reason")


@dataclass(frozen=True)
class Result:
    fixture_id: str
    status: Literal["pass", "fail", "pending"]
    evidence: tuple[str, ...]
    pending_reason: PendingReason | None = None

    def __post_init__(self) -> None:
        if self.pending_reason is not None and self.status != "pending":
            raise ValueError("pending declarations cannot certify a probe")


@dataclass(frozen=True)
class Adapter:
    driver: ManagedAgents
    store: StateStore | None
    transport: ScriptedTransport
    pending: Mapping[str, PendingReason] = field(default_factory=dict[str, PendingReason])

    def __post_init__(self) -> None:
        declarations = dict(self.pending)
        ids = {f"C{i:02}" for i in range(1, 19)}
        if any(
            key not in ids or type(reason) is not PendingReason
            for key, reason in declarations.items()
        ):
            raise ValueError("pending declarations need valid fixture IDs and typed reasons")
        object.__setattr__(self, "pending", MappingProxyType(declarations))


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
    for fixture_id in FIXTURES:
        adapter = registry.create(name)
        results.append(await run_fixture(fixture_id, adapter))
    return tuple(results)


async def run_fixture(fixture_id: str, adapter: Adapter) -> Result:
    """Declared gaps defer execution; undeclared provider errors still fail."""
    from mux.conformance.fixtures import FIXTURES

    if fixture_id not in FIXTURES:
        raise ValueError("unknown conformance fixture")
    reason = adapter.pending.get(fixture_id)
    if reason is not None:
        return Result(
            fixture_id,
            "pending",
            (f"adapter pending ({reason.kind.value}): {reason.detail}",),
            pending_reason=reason,
        )
    try:
        return await FIXTURES[fixture_id](adapter.driver, adapter.store, adapter.transport)
    except ConformanceFailure as exc:
        return Result(fixture_id, "fail", (f"check failed: {exc}",))
    except Exception as exc:
        # Do not serialize provider exception text, which can contain credentials.
        return Result(fixture_id, "fail", (f"probe raised {type(exc).__name__}",))
