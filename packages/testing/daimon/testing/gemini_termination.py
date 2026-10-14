"""Explicit Gemini C18 host envelopes; partial native evidence stays PENDING.

The driver owns the reachable-reason inventory, independently of the supplied
executions. The public N4 collector reads actual host finalizers and committed
outcome rows. Neither an envelope nor a provider callback supplies a verdict.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import cast

from daimon.core.turn.termination import TerminationReason
from daimon.testing.conformance.runner import (
    Adapter,
    PendingKind,
    PendingReason,
    Scenario,
    ScriptedTransport,
    SendEvidence,
)
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.turn_conformance import (
    TerminationCase,
    TerminationEvidence,
    termination_evidence,
)
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.resources import SkillUpload
from mux.errors import ScopeViolation
from mux.state.memory import CrashPoint
from mux.state.store import StateStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Source audit: common admission/queue/preparation gates plus driver finalizers
# and the public prepared-turn deadline remain reachable. Gemini has neither
# legacy retry/MCP-degraded records nor the Anthropic-only session replacement
# branch. History replay retains the session and never sets recovered=True.
# See GEMINI-TERMINATION.md for the independent source inventory and proof gaps.
UNREACHABLE_REASONS = frozenset(
    {
        TerminationReason.REDUCER_BUG,
        TerminationReason.MCP_DEGRADED_EMPTY,
        TerminationReason.RETRYING_UNSETTLED,
        TerminationReason.RECOVERY_CANCELLED,
        TerminationReason.RECOVERY_FAILED,
        TerminationReason.SESSION_PREPARATION_FAILED,
        TerminationReason.SESSION_AGENT_MISMATCH,
    }
)
REQUIRED_REASONS = (
    TerminationReason.COMPLETED,
    TerminationReason.INTERRUPTED,
    TerminationReason.INTERRUPT_TIMEOUT,
    TerminationReason.CONNECTION_LOST,
    TerminationReason.UPSTREAM,
    TerminationReason.RATE_LIMITED,
    TerminationReason.SESSION_TERMINATED,
    TerminationReason.REQUIRES_ACTION,
    TerminationReason.CEILING,
    TerminationReason.ADMISSION_BALANCE_DEPLETED,
    TerminationReason.ADMISSION_CAP_EXCEEDED,
    TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED,
    TerminationReason.ADMISSION_CHANNEL_PROTECTED,
    TerminationReason.ADMISSION_AGENT_PINNED_ELSEWHERE,
    TerminationReason.ADMISSION_CHANNEL_ISOLATED,
    TerminationReason.ADMISSION_DENIED,
    TerminationReason.ADMISSION_CONCURRENCY_SHED,
    TerminationReason.MISSING_CONFIG,
    TerminationReason.RESOLVER_MISS,
    TerminationReason.SESSION_BUSY,
    TerminationReason.UNKNOWN,
)


class _OwnedEvidenceSessions(async_sessionmaker[AsyncSession]):
    """Snapshot admitted routing; check every writer/read session allocation."""

    def __init__(self, source: async_sessionmaker[AsyncSession]) -> None:
        require_owned_sessionmaker(source)
        options = dict(source.kw)
        routes = options.get("binds")
        if isinstance(routes, Mapping):
            options["binds"] = dict(cast(Mapping[object, object], routes))
        super().__init__(class_=source.class_, **options)
        require_owned_sessionmaker(self)

    def __call__(self, **local_kw: object) -> AsyncSession:
        # The public collector's queued writer and SELECT use this factory,
        # independently of a callback reconfiguring its host's original one.
        require_owned_sessionmaker(self)
        if local_kw:
            raise ValueError("Gemini evidence sessions do not accept routing overrides")
        return super().__call__()


@dataclass(frozen=True)
class GeminiTerminationEnvelope:
    """Authorized executions using the real Gemini host/driver path.

    Declare expected identities, errors and recovery independently of execution.
    Missing executions keep the complete inventory visible, including failures
    before a native session exists. Callbacks return the actual host outcome or
    terminal lifecycle state, never hand-authored persisted facts.
    """

    sessionmaker: async_sessionmaker[AsyncSession]
    scope: Scope
    platform: str
    cases: tuple[TerminationCase, ...]

    def __post_init__(self) -> None:
        require_owned_sessionmaker(self.sessionmaker)
        object.__setattr__(self, "cases", tuple(self.cases))
        if not self.platform.strip():
            raise ValueError("Gemini termination requires a platform")
        if self.scope.is_platform or self.scope.is_legacy_host_authorized:
            raise ScopeViolation(self.scope.tenant_id, "Gemini termination requires native scope")
        names = tuple(case.name for case in self.cases)
        if not names or len(set(names)) != len(names) or any(not name.strip() for name in names):
            raise ValueError("Gemini termination requires distinct nonempty case names")
        for case in self.cases:
            if (
                type(case.expected) is not TerminationReason
                or case.expected not in REQUIRED_REASONS
            ):
                raise ValueError("Gemini termination case is outside the reachable inventory")
            ref = case.session
            if ref is None and case.expected in (
                TerminationReason.COMPLETED,
                TerminationReason.SESSION_TERMINATED,
            ):
                raise ValueError("completed and terminated Gemini cases must retain their session")
            if ref is not None and (
                ref.provider != "gemini"
                or ref.kind != "session"
                or ref.tenant_id != self.scope.tenant_id
                or ref.account_id != self.scope.account_id
            ):
                raise ScopeViolation(ref.id, "Gemini termination has a foreign native session")

    @property
    def missing_reasons(self) -> tuple[TerminationReason, ...]:
        represented = {case.expected for case in self.cases}
        return tuple(reason for reason in REQUIRED_REASONS if reason not in represented)

    async def evidence(self) -> TerminationEvidence:
        sessions = _OwnedEvidenceSessions(self.sessionmaker)
        return await termination_evidence(
            sessions,
            scope=self.scope,
            platform=self.platform,
            required_reasons=REQUIRED_REASONS,
            cases=self.cases,
        )


type EvidenceHook = Callable[[], Awaitable[Mapping[str, object]]]


@dataclass(frozen=True)
class GeminiTerminationTransport:
    """Delegate the existing provider adapter, adding only the explicit host hook."""

    native: ScriptedTransport
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


def compose_gemini_termination(
    adapter: Adapter, *, termination: GeminiTerminationEnvelope | None = None
) -> Adapter:
    """Explicit opt-in only; absent/partial evidence preserves typed C18 PENDING.

    Full declared execution only permits the shared fixture to judge facts. It
    is not certification: missing/wrong committed rows still fail its checks.
    Other fixture declarations, native controls and default registration stay
    unchanged. This testing module is never imported by application code.
    """
    capabilities = adapter.driver.capabilities()
    if capabilities.provider != "gemini" or capabilities.profile_id != "gemini.inline_reuse":
        raise ValueError("Gemini termination requires the explicit Gemini adapter")

    async def terminated() -> Mapping[str, object]:
        if termination is None:
            raise ValueError("Gemini termination envelope is absent")
        return await termination.evidence()

    declarations = dict(adapter.pending)
    declarations.setdefault(
        "C18",
        PendingReason(
            PendingKind.ADAPTER_DEPENDENCY, "Gemini host termination envelope is absent."
        ),
    )
    if termination is not None:
        if termination.missing_reasons:
            declarations["C18"] = PendingReason(
                PendingKind.ADAPTER_DEPENDENCY,
                "Gemini host termination execution is partial; missing: "
                + ", ".join(reason.value for reason in termination.missing_reasons),
            )
        else:
            declarations.pop("C18", None)
    return replace(
        adapter,
        transport=GeminiTerminationTransport(
            adapter.transport, terminated if termination is not None else None
        ),
        pending=declarations,
    )
