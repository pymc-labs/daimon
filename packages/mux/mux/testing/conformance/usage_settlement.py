"""Pure normalized usage evidence: no key, transport or provider I/O."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import JsonValue

from mux.contracts.ids import ResourceRef
from mux.contracts.usage import UsageObservation
from mux.testing.conformance.budget import (
    ActualSpend,
    BudgetGuard,
    BudgetLedgerError,
    ContainerUsage,
    MeasuredRequest,
    Reservation,
    SpendReceipt,
    TokenUsage,
)


def actual_from_observations(
    observations: Iterable[UsageObservation],
    *,
    provider: str,
    model: str,
    containers: tuple[ContainerUsage, ...] | None,
    usage_complete: bool,
    pricing_basis: str | None = None,
) -> ActualSpend:
    """Only disjoint model-request increments; never sum overlapping grains.

    A repeated ID, even a correction, requires the caller to complete its
    revision walk first. Null buckets remain null; nothing defaults to zero.
    """
    requests: list[MeasuredRequest] = []
    all_measured = True
    for observation in observations:
        if (
            observation.model is None
            or observation.model.provider != provider
            or observation.model.id != model
            or observation.grain != "model_request"
            or observation.basis != "increment"
            or observation.covers
        ):
            raise BudgetLedgerError("usage evidence is foreign or overlapping")
        all_measured = all_measured and observation.completeness == "measured"
        tokens = TokenUsage(
            input_tokens=observation.input_tokens,
            output_tokens=observation.output_tokens,
            input_cached_tokens=observation.input_cached_tokens,
            input_cache_write_tokens=observation.input_cache_write_tokens,
        )
        if provider == "anthropic":
            duration = observation.native_meter.get("cache_creation")
            if isinstance(duration, Mapping):
                tokens = TokenUsage.model_validate(
                    {
                        **tokens.model_dump(),
                        "input_cache_write_5m_tokens": duration.get("ephemeral_5m_input_tokens"),
                        "input_cache_write_1h_tokens": duration.get("ephemeral_1h_input_tokens"),
                    }
                )
        requests.append(
            MeasuredRequest(
                id=observation.id,
                observed_at=observation.observed_at,
                pricing_basis=pricing_basis,
                tokens=tokens,
            )
        )
    return ActualSpend(
        requests=tuple(requests),
        containers=containers,
        usage_complete=usage_complete and all_measured,
    )


@dataclass(frozen=True)
class AnthropicSessionSpend:
    """One native session's verified model-request walk and running-time sum.

    The runner establishes served model, finality, complete running intervals
    and absence of unpriced auxiliary work before claiming usage_complete.
    Observations use the shared driver normalizer with that verified model;
    no model or missing runtime is inferred here. This is evidence, not a live
    authorization, native budget acknowledgement or execution control.
    """

    session: ResourceRef
    observations: tuple[UsageObservation, ...]
    runtime: ContainerUsage | None
    usage_complete: bool = False


def actual_from_anthropic_sessions(
    reservation: Reservation,
    sessions: Iterable[AnthropicSessionSpend],
    *,
    pricing_basis: str | None = None,
) -> ActualSpend:
    """Combine a reserved single/pair session walk for ONE guard settlement.

    Missing sessions, finality or runtime keep the reservation held. Foreign,
    stale and overlapping identities refuse before any ledger append. A complete
    walk can contain zero model requests, but never imply zero unknown runtime.
    """
    receipt = reservation.receipt
    allowance = receipt.container_allowance
    if (
        receipt.provider != "anthropic"
        or allowance is None
        or allowance.meter != "agent_session"
        or allowance.memory_gb is not None
        or allowance.sessions < 1
        or receipt.limits is None
    ):
        raise BudgetLedgerError("Anthropic session evidence needs an agent_session reservation")
    sessions = tuple(sessions)
    refs = [item.session for item in sessions]
    if len({ref.id for ref in refs}) != len(refs) or len(refs) > allowance.sessions:
        raise BudgetLedgerError("duplicate or excess session evidence")
    if len({(ref.account_scope_id, ref.tenant_id, ref.account_id) for ref in refs}) > 1:
        raise BudgetLedgerError("session evidence crosses account scopes")
    complete = len(sessions) == allowance.sessions
    requests: list[MeasuredRequest] = []
    containers: list[ContainerUsage] = []
    runtime_complete = complete
    for item in sessions:
        if type(item.usage_complete) is not bool:
            raise BudgetLedgerError("session completeness must be an explicit boolean")
        ref = item.session
        if ref.provider != "anthropic" or ref.kind != "session":
            raise BudgetLedgerError("foreign session evidence")
        if any(
            obs.session != ref
            or obs.observed_at.tzinfo is None
            or obs.observed_at < receipt.timestamp
            for obs in item.observations
        ):
            raise BudgetLedgerError("foreign or stale request evidence")
        evidence = actual_from_observations(
            item.observations,
            provider="anthropic",
            model=receipt.model,
            containers=None,
            usage_complete=item.usage_complete,
            pricing_basis=pricing_basis,
        )
        requests.extend(evidence.requests)
        complete = complete and evidence.usage_complete
        runtime = item.runtime
        if runtime is None:
            runtime_complete = False
        elif (
            runtime.id != ref.id
            or runtime.meter != "agent_session"
            or runtime.memory_gb is not None
            or runtime.started_at.tzinfo is None
            or runtime.started_at < receipt.timestamp
        ):
            raise BudgetLedgerError("foreign or stale runtime evidence")
        else:
            containers.append(runtime)
    try:
        return ActualSpend(
            requests=tuple(requests),
            # Retain known intervals as lower bounds even when another is
            # missing. Global completeness fences exact settlement in that case.
            containers=tuple(containers) if containers else None,
            usage_complete=complete and runtime_complete,
        )
    except ValueError:
        raise BudgetLedgerError("overlapping session request evidence") from None


def settle_anthropic_events(
    guard: BudgetGuard,
    reservation: Reservation,
    events: Iterable[Mapping[str, JsonValue]],
    session: ResourceRef,
    *,
    observed_at: datetime,
    containers: tuple[ContainerUsage, ...] | None,
    usage_complete: bool,
    pricing_basis: str | None = None,
    status: Literal["completed", "failed", "cancelled"] = "completed",
) -> SpendReceipt:
    """Normalize MA tokens with a reserved and explicitly measured session.

    Missing, empty or ambiguous session measurements leave runtime unknown;
    an empty tuple never proves zero running time on this session path.
    """
    from mux.drivers.anthropic.usage import observation_from_event

    if reservation.receipt.provider != "anthropic" or session.provider != "anthropic":
        raise BudgetLedgerError("Anthropic settlement requires an Anthropic run")
    allowance = reservation.receipt.container_allowance
    if allowance is None or allowance.meter != "agent_session":
        raise BudgetLedgerError("Anthropic settlement requires an agent_session allowance")
    if containers is None or len(containers) != 1 or containers[0].meter != "agent_session":
        containers = None
    requests: list[MeasuredRequest] = []
    for event in events:
        observation = observation_from_event(
            event, session, observed_at=observed_at, model_id=reservation.receipt.model
        )
        evidence = actual_from_observations(
            (observation,),
            provider="anthropic",
            model=reservation.receipt.model,
            containers=(),
            usage_complete=True,
            pricing_basis=pricing_basis,
        )
        tokens = evidence.requests[0].tokens
        # The shared normalizer preserves the native duration split for auditing.
        # Missing durations remain unknown instead of claiming the cheap 5m tier.
        duration = observation.native_meter.get("cache_creation")
        if isinstance(duration, Mapping):
            tokens = TokenUsage.model_validate(
                {
                    **tokens.model_dump(),
                    "input_cache_write_5m_tokens": duration.get("ephemeral_5m_input_tokens"),
                    "input_cache_write_1h_tokens": duration.get("ephemeral_1h_input_tokens"),
                }
            )
        requests.append(
            MeasuredRequest(
                id=observation.id,
                observed_at=observed_at,
                tokens=tokens,
                pricing_basis=pricing_basis,
            )
        )
    if reservation.receipt.limits is None:
        raise BudgetLedgerError("Anthropic settlement needs pinned limits")
    return guard.settle(
        reservation,
        status=status,
        limits=reservation.receipt.limits,
        actual=ActualSpend(
            requests=tuple(requests), containers=containers, usage_complete=usage_complete
        ),
    )
