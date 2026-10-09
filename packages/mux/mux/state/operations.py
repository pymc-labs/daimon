"""Operations: idempotent intent, persisted before any provider I/O.

An operation is found by (tenant, account, key) and owned by the principal
that began it; another principal using the key gets `ScopeViolation`. The
first `begin` persists it as `pending`. Before issuing the request a caller
must win `claim`, the compare-and-swap from `pending` to `sent`: exactly one
caller wins, so exactly one sends. A `pending` operation was never sent; a
`sent` one may have been. The same key with the same request digest returns
the existing record; with a different digest (or slot) it raises
`OperationConflict`.

If reconciling a `sent` or `outcome_unknown` operation proves the provider
never received it, the operation goes back to `pending` and may be claimed
again under the same key. Only conclude absence once the claimer's lease has expired and its send
deadline has passed, or a request still in flight lands after the resend.
So a driver keeps its send timeout below the lease TTL and never starts
I/O on a lease past its expiry.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue

from mux.contracts._base import Contract, FrozenMap
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.receipts import Operation, OperationStatus
from mux.errors import MuxError, OperationConflict, ScopeViolation
from mux.state.lease import Slot

TRANSITIONS: Mapping[OperationStatus, frozenset[OperationStatus]] = {
    "pending": frozenset({"sent", "failed"}),
    "sent": frozenset({"pending", "accepted", "processed", "outcome_unknown", "failed"}),
    "accepted": frozenset({"processed", "outcome_unknown", "failed"}),
    "outcome_unknown": frozenset({"pending", "accepted", "processed", "failed"}),
    "processed": frozenset(),
    "failed": frozenset(),
}
"""Allowed moves. `pending → sent` happens only through `claim`. `pending`
may fail without being sent (a refused request). Back to `pending` only
when reconciling proved the provider never got the request."""

Recovery = Literal["send", "reconcile", "observe", "done"]


class InvalidTransition(MuxError):
    """An operation was moved along an edge `TRANSITIONS` does not allow."""

    def __init__(self, key: str, current: OperationStatus, target: OperationStatus) -> None:
        super().__init__(f"operation {key!r} cannot move from {current} to {target}")
        self.key = key
        self.current: OperationStatus = current
        self.target: OperationStatus = target


class SendClaimed(MuxError):
    """`claim` lost: the operation is no longer `pending`, so someone else sends."""

    def __init__(self, key: str, status: OperationStatus) -> None:
        super().__init__(f"operation {key!r} is {status}, not pending")
        self.key = key
        self.status: OperationStatus = status


class OperationRecord(Contract):
    """An operation, who owns it and the slot it writes to, if any.

    `result` holds what the driver needs to rebuild its receipt (input ids,
    turn id) when the key is replayed.
    """

    tenant_id: str
    account_id: str
    principal_id: str
    slot: Slot | None = None
    operation: Operation
    result: FrozenMap[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


class Begun(Contract):
    """`fresh` is False when the key already existed with the same request."""

    record: OperationRecord
    fresh: bool


def request_digest(request: JsonValue) -> str:
    """The sha256 of the request's canonical JSON (sorted keys, no spaces)."""
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def operation_scope(scope: Scope) -> tuple[str, str]:
    """The part of a scope an operation key is looked up within.

    Not the authorization: a retry after a crash may carry a fresh one and
    must still find its operation. The principal is checked, not looked up
    by, so a reused key is refused instead of silently forked.
    """
    return (scope.tenant_id, scope.account_id)


def check_owner(record: OperationRecord, scope: Scope) -> None:
    if (record.tenant_id, record.account_id, record.principal_id) != (
        scope.tenant_id,
        scope.account_id,
        scope.principal_id,
    ):
        raise ScopeViolation(record.operation.id, "operation belongs to another principal")


def begin(
    existing: OperationRecord | None,
    scope: Scope,
    *,
    key: str,
    request_digest: str,
    operation_id: str,
    now: datetime,
    slot: Slot | None = None,
) -> Begun:
    if slot is not None and (
        slot.tenant_id != scope.tenant_id or slot.account_id not in (None, scope.account_id)
    ):
        raise ScopeViolation(slot.describe(), "slot is outside the caller's scope")
    if existing is not None:
        check_owner(existing, scope)
        if existing.operation.request_digest != request_digest or existing.slot != slot:
            raise OperationConflict(key)
        return Begun(record=existing, fresh=False)
    tenant_id, account_id = operation_scope(scope)
    operation = Operation(
        id=operation_id,
        key=key,
        request_digest=request_digest,
        status="pending",
        created_at=now,
        updated_at=now,
    )
    record = OperationRecord(
        tenant_id=tenant_id,
        account_id=account_id,
        principal_id=scope.principal_id,
        slot=slot,
        operation=operation,
    )
    return Begun(record=record, fresh=True)


def _moved(
    record: OperationRecord,
    status: OperationStatus,
    now: datetime,
    resource: ResourceRef | None = None,
    result: Mapping[str, JsonValue] | None = None,
) -> OperationRecord:
    current = record.operation
    operation = current.model_copy(
        update={
            "status": status,
            "updated_at": now,
            "resource": resource if resource is not None else current.resource,
        }
    )
    merged = {**record.result, **(result or {})}
    return OperationRecord.model_validate(
        {**record.model_dump(), "operation": operation.model_dump(), "result": merged}
    )


def claim(record: OperationRecord, *, now: datetime) -> OperationRecord:
    """`pending → sent`, or `SendClaimed`. Only the winner sends."""
    if record.operation.status != "pending":
        raise SendClaimed(record.operation.key, record.operation.status)
    return _moved(record, "sent", now)


def advance(
    record: OperationRecord,
    status: OperationStatus,
    *,
    now: datetime,
    resource: ResourceRef | None = None,
    result: Mapping[str, JsonValue] | None = None,
) -> OperationRecord:
    """Record what was observed. Repeating an observed status is a no-op, so
    a replayed acknowledgement is harmless; `sent` is reached only by `claim`."""
    current = record.operation
    if status == "sent" or (status == "pending" and current.status == "pending"):
        raise InvalidTransition(current.key, current.status, status)
    if status == current.status:
        return record
    if status not in TRANSITIONS[current.status]:
        raise InvalidTransition(current.key, current.status, status)
    return _moved(record, status, now, resource, result)


def recovery(operation: Operation) -> Recovery:
    """What a successor does with an operation it finds after a crash.

    `pending` was never sent: claim it, and send only if the claim wins.
    `sent` and `outcome_unknown` may have reached the provider: reconcile,
    never resend. `accepted` is known upstream: observe its outcome.
    """
    match operation.status:
        case "pending":
            return "send"
        case "sent" | "outcome_unknown":
            return "reconcile"
        case "accepted":
            return "observe"
        case "processed" | "failed":
            return "done"
