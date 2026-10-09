"""Interpret independent authoritative stop evidence after a cancel request.

Anthropic reports an idle session rather than a distinct interrupt outcome.
The cancel context plus an observed root idle proves that the requested stop
completed. The request receipt, its status and interrupt echoes never do.
"""

from mux.contracts.events import Event
from mux.contracts.receipts import CancelReceipt, StopObservation
from mux.errors import ScopeViolation


def observed_stop(event: Event, receipt: CancelReceipt) -> StopObservation | None:
    if event.session_id != receipt.session.id or event.native.provider != receipt.session.provider:
        raise ScopeViolation(event.session_id, "stop evidence belongs to another session")
    if event.authority not in {"record", "reconciled"}:
        return None
    if event.type not in {
        "session.turn_ended",
        "session.requires_action",
        "session.status_terminated",
    }:
        return None
    return StopObservation(
        receipt_operation_id=receipt.operation_id,
        stopped=True,
        outcome="terminated" if event.type == "session.status_terminated" else "interrupted",
        observed_at=event.observed_at,
    )
