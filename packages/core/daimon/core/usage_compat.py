"""Temporary M0 SDK event boundary for callers owned by other sprint lanes.

No provider requests. Remove when turn and adapter callers supply observations.
"""

from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
from mux.contracts.ids import ResourceRef
from mux.contracts.usage import UsageObservation
from mux.drivers.anthropic.usage import observation_from_event


def event_observation(
    event: BetaManagedAgentsSpanModelRequestEndEvent,
    *,
    session_id: str,
    model_id: str | None = None,
    tenant_id: str | None = None,
) -> UsageObservation:
    return observation_from_event(
        event.model_dump(mode="json"),
        session=ResourceRef(
            id=session_id,
            kind="session",
            provider="anthropic",
            account_scope_id="legacy-host",
            tenant_id=tenant_id,
        ),
        observed_at=event.processed_at,
        model_id=model_id,
    )
