"""Explicit OpenAI host envelopes for the shared conformance oracle.

This opt-in QA composition has no application registration or client discovery.
Callbacks supply native host execution; the owning shared hooks query durable
facts and the mux fixtures decide the result. An absent envelope stays pending.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Literal, cast

from daimon.testing.accounting_conformance import HostAccountingHook
from daimon.testing.openai_termination import OpenAITerminationEnvelope
from daimon.testing.turn_conformance import (
    BackendSelectionEnvelope,
    Evidence,
    WakeEnvelope,
    backend_selection_evidence,
    wake_evidence,
)
from mux.contracts.ids import ResourceRef
from mux.contracts.resources import SkillUpload
from mux.errors import ScopeViolation
from mux.state.memory import CrashPoint
from mux.state.store import StateStore
from mux.testing.conformance.runner import (
    Adapter,
    PendingKind,
    PendingReason,
    Scenario,
    ScriptedTransport,
    SendEvidence,
)

type EvidenceHook = Callable[[], Awaitable[Evidence]]
PROFILE = "openai.persistent_workspace"
C12_EXCLUSION = PendingReason(
    PendingKind.CAPABILITY_UNAVAILABLE,
    "OpenAI immutable-disjoint usage coverage refuses the shared C12 overlapping "
    "turn/session aggregates; queried host facts are supplemental only.",
)
HOST_GAPS = {
    "C14": PendingReason(PendingKind.ADAPTER_DEPENDENCY, "OpenAI host selection envelope absent"),
    "C17": PendingReason(PendingKind.ADAPTER_DEPENDENCY, "OpenAI host wake envelope absent"),
    "C18": PendingReason(
        PendingKind.ADAPTER_DEPENDENCY,
        "N4 complete outcome API and OpenAI reachable-reason coverage are not installed",
    ),
}


@dataclass(frozen=True)
class SelectionFinding:
    """Observed binding-pin loss, routed to its host selector owner."""

    expected_identity: tuple[object, ...]
    prepared_identity: tuple[object, ...]
    persisted_identity: object
    kind: Literal["existing_thread_binding_changed"] = "existing_thread_binding_changed"
    owner: Literal["N4"] = "N4"


@dataclass(frozen=True)
class OpenAIHostTransport:
    """Delegate provider controls while exposing only explicitly supplied hooks."""

    native: ScriptedTransport
    host_accounting_evidence: EvidenceHook | None = None
    host_backend_selection_evidence: EvidenceHook | None = None
    host_wake_evidence: EvidenceHook | None = None
    host_termination_evidence: EvidenceHook | None = None

    async def arrange(self, fixture_id: str) -> Scenario:
        return await self.native.arrange(fixture_id)

    def fault(self, name: str) -> None:
        self.native.fault(name)

    @property
    def mutation_count(self) -> int:
        return self.native.mutation_count

    @property
    def deleted_resources(self) -> tuple[ResourceRef, ...]:
        return self.native.deleted_resources

    @property
    def skill_uploads(self) -> tuple[SkillUpload, ...]:
        return self.native.skill_uploads

    @property
    def upstream_sends(self) -> tuple[SendEvidence, ...]:
        return self.native.upstream_sends

    @property
    def reconciled_sends(self) -> tuple[SendEvidence, ...]:
        return self.native.reconciled_sends

    def restart_store(
        self, store: StateStore, *, crash: Mapping[str, CrashPoint] | None = None
    ) -> StateStore:
        return self.native.restart_store(store, crash=crash)


def compose_openai_host(
    adapter: Adapter,
    *,
    accounting: HostAccountingHook | None = None,
    selection: BackendSelectionEnvelope | None = None,
    wake: WakeEnvelope | None = None,
    termination: OpenAITerminationEnvelope | None = None,
) -> Adapter:
    """Install host facts, preserving the C12 exclusion and absent-hook reasons.

    Selection callbacks must invoke normal admission with current channel config;
    no old-revision override is supplied here. Wake callbacks call the normal
    host driver under N4's active persistence context (no second lease wrapper).
    C18 uses the public committed-outcome API. Partial declared coverage retains
    PENDING while exposing the available snapshots for diagnosis.
    """
    if adapter.driver.capabilities().profile_id != PROFILE:
        raise ValueError("OpenAI host conformance requires the explicit persistent profile")
    if accounting is not None and (
        accounting.session.provider != "openai" or accounting.model.provider != "openai"
    ):
        raise ValueError("OpenAI accounting envelope requires OpenAI native identities")
    if wake is not None and (
        wake.session.provider != "openai"
        or wake.replacement.provider != "openai"
        or wake.replacement.profile != PROFILE
    ):
        raise ValueError("OpenAI wake envelope requires an OpenAI binding and session")

    async def selected() -> Evidence:
        if selection is None:
            raise ValueError("selection envelope is absent")
        if (
            selection.scope.is_platform
            or selection.scope.is_legacy_host_authorized
            or selection.channel.tenant_id != selection.scope.tenant_id
        ):
            raise ScopeViolation(selection.channel.channel_id, "foreign selection envelope")
        original = await selection.store.latest_config_revision(selection.channel)
        if original is None or original.profile != PROFILE:
            raise ValueError("OpenAI selection envelope must start at its explicit profile")
        facts = await backend_selection_evidence(selection)
        first, existing = facts.get("first"), facts.get("existing")
        if isinstance(first, tuple) and isinstance(existing, tuple) and first != existing:
            return dict(facts) | {
                "finding": SelectionFinding(
                    cast(tuple[object, ...], first),
                    cast(tuple[object, ...], existing),
                    facts.get("existing_binding"),
                )
            }
        return facts

    async def awakened() -> Evidence:
        if wake is None:
            raise ValueError("wake envelope is absent")
        return await wake_evidence(wake)

    async def terminated() -> Evidence:
        if termination is None:
            raise ValueError("termination envelope is absent")
        return await termination.evidence()

    declarations = dict(adapter.pending)
    declarations["C12"] = C12_EXCLUSION
    for fid, hook in (("C14", selection), ("C17", wake)):
        if hook is not None:
            declarations.pop(fid, None)
        else:
            declarations.setdefault(fid, HOST_GAPS[fid])
    declarations.setdefault("C18", HOST_GAPS["C18"])
    if termination is not None:
        if termination.missing_reasons:
            declarations["C18"] = PendingReason(
                PendingKind.ADAPTER_DEPENDENCY,
                "OpenAI host termination execution is partial; missing: "
                + ", ".join(reason.value for reason in termination.missing_reasons),
            )
        else:
            declarations.pop("C18", None)
    transport = OpenAIHostTransport(
        adapter.transport,
        accounting,
        selected if selection is not None else None,
        awakened if wake is not None else None,
        terminated if termination is not None else None,
    )
    return replace(adapter, transport=transport, pending=declarations)
