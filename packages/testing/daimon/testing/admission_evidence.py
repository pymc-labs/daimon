"""Opt-in host admission capture, independent of provider completion evidence.

The caller runs its real adapter path with the supplied core observer, and
supplies independently captured platform effects and native-work counters.
Missing coverage stays PENDING; this hook never renders a refusal or assigns
a provider session/root to an attempt which was denied before native work.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from daimon.core.turn.admission import AdmissionDecision, AdmissionObserver
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.outcome_oracle import LogObservation, ReactionEffect, VisibleText
from mux.contracts.ids import Provider, Scope


@dataclass(frozen=True)
class AdmissionEffects:
    """Actual adapter observations in the attempt's owned capture window.

    Completion covers the installed capture boundary, not live gateway timing.
    A complete empty notice/reaction tuple faithfully records a silent refusal.
    """

    notices: tuple[VisibleText, ...] = ()
    logs: tuple[LogObservation, ...] = ()
    reactions: tuple[ReactionEffect, ...] = ()
    complete: bool = False


type AdmissionGap = Literal[
    "HOST_ADMISSION_NOT_REACHED",
    "NATIVE_SEND_COVERAGE_MISSING",
    "ADAPTER_EFFECT_COVERAGE_MISSING",
]


@dataclass(frozen=True)
class AdmissionReceipt:
    evidence_id: str
    backend: Provider
    scope: Scope
    platform: str
    channel_id: str
    thread_id: str | None
    external_user_id: str
    started_s: float
    observed_s: float
    decision_s: float | None
    decision: AdmissionDecision | None
    effects: AdmissionEffects
    native_sends: int | None
    pending: tuple[AdmissionGap, ...]


async def admission_edge(
    *,
    deps: TurnDeps,
    scope: Scope,
    backend: Provider,
    platform: str,
    channel_id: str,
    external_user_id: str,
    evidence_id: str,
    invoke: Callable[[AdmissionObserver], Awaitable[object]],
    effects: Callable[[], AdmissionEffects],
    native_send_count: Callable[[], int | None],
    thread_id: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AdmissionReceipt:
    """Observe one real host attempt without changing its policy or dispatch.

    ``invoke(observer)`` installs that observer on core ``admit`` and runs the
    adapter through its actual refusal handler. Plain core refusals are also
    accepted, but their absent platform coverage remains PENDING. Counters
    cover session creation / model input sends across the whole invocation,
    excluding metadata reads. None means unknown, never an inferred zero.
    """
    require_owned_sessionmaker(deps.sessionmaker)
    if (
        scope.is_platform
        or scope.is_legacy_host_authorized
        or not all((scope.tenant_id, scope.account_id, scope.principal_id, scope.authorization_id))
        or backend not in ("anthropic", "openai", "gemini")
        or not all((platform, channel_id, external_user_id, evidence_id))
    ):
        raise ValueError("Admission capture requires an authorized tenant and attempt identity")

    def timestamp() -> float:
        value = clock()
        if not math.isfinite(value) or value < 0:
            raise ValueError("Invalid admission capture clock")
        return value

    def count() -> int | None:
        value = native_send_count()
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("Native send count must be a nonnegative integer or unknown")
        return value

    started = timestamp()
    before = count()
    decisions: list[tuple[AdmissionDecision, float]] = []

    def observer(decision: AdmissionDecision) -> None:
        # Record first; validate outside the passive core observer's error edge.
        decisions.append((decision, clock()))

    denial: AdmissionDenied | None = None
    try:
        await invoke(observer)
    except AdmissionDenied as exc:
        denial = exc
    observed = timestamp()
    after = count()
    if observed < started:
        raise ValueError("Admission clock moved backwards")
    if before is not None and after is not None and after < before:
        raise ValueError("Native send counter moved backwards")
    sends = after - before if before is not None and after is not None else None
    if len(decisions) > 1:
        raise ValueError("Admission capture covers exactly one attempt")
    decision, decision_s = decisions[0] if decisions else (None, None)
    if decision is not None:
        if (
            str(decision.tenant_id) != scope.tenant_id
            or decision.platform != platform
            or decision.channel_id != channel_id
            or decision.thread_id != thread_id
            or decision.external_user_id != external_user_id
            or (decision.account_id is not None and str(decision.account_id) != scope.account_id)
        ):
            raise ValueError("Admission decision is outside the authorized attempt")
        if (
            decision_s is None
            or not math.isfinite(decision_s)
            or not started <= decision_s <= observed
        ):
            raise ValueError("Admission decision is outside the capture window")
        if denial is not None and decision.reason != denial.reason:
            raise ValueError("Admission observer disagrees with the actual refusal")
    captured = effects()
    for effect in (*captured.notices, *captured.logs, *captured.reactions):
        if not started <= effect.observed_s <= observed:
            raise ValueError("Adapter effect is outside the admission capture window")
    pending: list[AdmissionGap] = []
    if decision is None:
        pending.append("HOST_ADMISSION_NOT_REACHED")
    if sends is None:
        pending.append("NATIVE_SEND_COVERAGE_MISSING")
    if not captured.complete:
        pending.append("ADAPTER_EFFECT_COVERAGE_MISSING")
    return AdmissionReceipt(
        evidence_id,
        backend,
        scope,
        platform,
        channel_id,
        thread_id,
        external_user_id,
        started,
        observed,
        decision_s,
        decision,
        captured,
        sends,
        tuple(pending),
    )
