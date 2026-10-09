"""Receipts: what a mutating call reports back.

Every mutating port call takes an operation `key`. The state store persists
the intent before any provider I/O; the same key with the same request
returns the existing receipt, and with a different request raises
`OperationConflict`. A delivery the driver cannot confirm reports
`outcome_unknown` instead of resending.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from mux.contracts._base import Contract
from mux.contracts.events import TurnOutcome
from mux.contracts.ids import ResourceRef

OperationStatus = Literal["pending", "sent", "accepted", "processed", "outcome_unknown", "failed"]


class Operation(Contract):
    id: str
    key: str
    request_digest: str
    status: OperationStatus
    resource: ResourceRef | None = None
    created_at: datetime
    updated_at: datetime


class SendReceipt(Contract):
    operation_id: str
    status: Literal["queued", "processed", "outcome_unknown"]
    input_ids: tuple[str, ...]
    turn_id: str | None = None


class CancelReceipt(Contract):
    """A cancel was requested. Whether the turn stopped is a separate observation."""

    operation_id: str
    session: ResourceRef
    turn_id: str
    status: Literal["requested", "already_stopped", "outcome_unknown"]
    requested_at: datetime


class StopObservation(Contract):
    receipt_operation_id: str
    stopped: bool
    outcome: TurnOutcome | None = None
    observed_at: datetime


class UpdateReceipt(Contract):
    operation_id: str
    status: OperationStatus
    applies: Literal["now", "next_turn", "replaced"]
    session: ResourceRef


class DeletionReceipt(Contract):
    """What a delete actually did, per resource. An archive is never reported as a delete."""

    operation_id: str
    deleted: tuple[ResourceRef, ...] = ()
    retained: tuple[ResourceRef, ...] = ()
    pending: tuple[ResourceRef, ...] = ()
    failed: tuple[ResourceRef, ...] = ()


class RestoreReceipt(Contract):
    operation_id: str
    session: ResourceRef
    accepted_losses: tuple[str, ...] = ()
