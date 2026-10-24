"""OpenAI C18 envelopes over the public committed host-outcome collector.

The inventory is independent of executed cases. Partial evidence is useful for
diagnosis, but never clears the adapter's C18 PENDING declaration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from daimon.core.turn.termination import TerminationReason
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.turn_conformance import (
    TerminationCase,
    TerminationEvidence,
    termination_evidence,
)
from mux.contracts.ids import Scope
from mux.errors import ScopeViolation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Owner audit: common admission/preparation exceptions, driver finalizers and
# run_prepared_turn's deadline boundary remain reachable. These exclusions
# require legacy records/legacy binding/replacement branches that OpenAI cannot
# enter, an Anthropic refusal idle (`refused`), or have no production raiser.
# Native history replay does not replace a host session and does not set
# RunOutcome.recovered. See the source inventory.
UNREACHABLE_REASONS = frozenset(
    {
        TerminationReason.REDUCER_BUG,
        TerminationReason.REFUSED,
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
            raise ValueError("OpenAI evidence sessions do not accept routing overrides")
        return super().__call__()


@dataclass(frozen=True)
class OpenAITerminationEnvelope:
    """An explicit owned database, authorization and independent native cases.

    Callbacks execute preparation/host turns; they return actual RunOutcome or
    lifecycle TurnState objects. They cannot supply persisted facts or verdicts.
    No caller-selected reachable list can shrink the provider inventory.
    """

    sessionmaker: async_sessionmaker[AsyncSession]
    scope: Scope
    platform: str
    cases: tuple[TerminationCase, ...]

    def __post_init__(self) -> None:
        require_owned_sessionmaker(self.sessionmaker)
        # Snapshot even a runtime caller passing a mutable list despite typing.
        object.__setattr__(self, "cases", tuple(self.cases))
        if not self.platform.strip():
            raise ValueError("OpenAI termination evidence requires a platform")
        if self.scope.is_platform or self.scope.is_legacy_host_authorized:
            raise ScopeViolation(self.scope.tenant_id, "OpenAI termination requires native scope")
        names = tuple(case.name for case in self.cases)
        if len(set(names)) != len(names) or any(not name.strip() for name in names):
            raise ValueError("OpenAI termination cases require distinct nonempty names")
        for case in self.cases:
            if (
                type(case.expected) is not TerminationReason
                or case.expected not in REQUIRED_REASONS
            ):
                raise ValueError("OpenAI termination case is outside the reachable inventory")
            ref = case.session
            if ref is not None and (
                ref.provider != "openai"
                or ref.kind != "session"
                or ref.tenant_id != self.scope.tenant_id
                or ref.account_id != self.scope.account_id
            ):
                raise ScopeViolation(ref.id, "OpenAI termination case has a foreign native session")

    @property
    def missing_reasons(self) -> tuple[TerminationReason, ...]:
        observed = {case.expected for case in self.cases}
        return tuple(reason for reason in REQUIRED_REASONS if reason not in observed)

    async def evidence(self) -> TerminationEvidence:
        sessions = _OwnedEvidenceSessions(self.sessionmaker)
        return await termination_evidence(
            sessions,
            scope=self.scope,
            platform=self.platform,
            required_reasons=REQUIRED_REASONS,
            cases=self.cases,
        )
