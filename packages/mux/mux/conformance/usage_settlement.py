"""Pure normalized usage evidence: no key, transport or provider I/O."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Literal

from pydantic import JsonValue

from mux.conformance.budget import (
    ActualSpend,
    BudgetGuard,
    BudgetLedgerError,
    ContainerUsage,
    MeasuredRequest,
    Reservation,
    SpendReceipt,
    TokenUsage,
)
from mux.contracts.ids import ResourceRef
from mux.contracts.usage import UsageObservation


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
        requests.append(
            MeasuredRequest(
                id=observation.id,
                observed_at=observation.observed_at,
                pricing_basis=pricing_basis,
                tokens=TokenUsage(
                    input_tokens=observation.input_tokens,
                    output_tokens=observation.output_tokens,
                    input_cached_tokens=observation.input_cached_tokens,
                    input_cache_write_tokens=observation.input_cache_write_tokens,
                ),
            )
        )
    return ActualSpend(
        requests=tuple(requests),
        containers=containers,
        usage_complete=usage_complete and all_measured,
    )


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
    """Use the driver's actual normalizer; only fixed token evidence is persisted."""
    from mux.drivers.anthropic.usage import observation_from_event

    if reservation.receipt.provider != "anthropic" or session.provider != "anthropic":
        raise BudgetLedgerError("Anthropic settlement requires an Anthropic run")
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
