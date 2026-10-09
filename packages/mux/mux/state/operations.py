"""Operations: idempotent intent, persisted before any provider I/O.

An operation is keyed by (tenant, account, key). The first `begin` persists
it as `pending`; the caller marks it `sent` *before* issuing the request, so
a `pending` operation was never sent and a `sent` one may have been. The
same key with the same request digest returns the existing record; with a
different digest it raises `OperationConflict`.
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
from mux.errors import MuxError, OperationConflict

TRANSITIONS: Mapping[OperationStatus, frozenset[OperationStatus]] = {
    "pending": frozenset({"sent", "failed"}),
    "sent": frozenset({"accepted", "processed", "outcome_unknown", "failed"}),
    "accepted": frozenset({"processed", "outcome_unknown", "failed"}),
    "outcome_unknown": frozenset({"accepted", "processed", "failed"}),
    "processed": frozenset(),
    "failed": frozenset(),
}
"""Allowed moves. `pending` may fail without being sent (a refused request);
`outcome_unknown` is resolved by reconciling, never by resending."""

Recovery = Literal["send", "reconcile", "observe", "done"]


class InvalidTransition(MuxError):
    """An operation was moved along an edge `TRANSITIONS` does not allow."""

    def __init__(self, key: str, current: OperationStatus, target: OperationStatus) -> None:
        super().__init__(f"operation {key!r} cannot move from {current} to {target}")
        self.key = key
        self.current: OperationStatus = current
        self.target: OperationStatus = target


class OperationRecord(Contract):
    """An operation and who owns it. `result` holds what the driver needs to
    rebuild its receipt (input ids, turn id) when the key is replayed."""

    tenant_id: str
    account_id: str
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
    """The part of a scope an operation key is unique within.

    Not the principal or authorization: a retry after a crash may carry a
    fresh authorization and must still find its operation.
    """
    return (scope.tenant_id, scope.account_id)


def begin(
    existing: OperationRecord | None,
    scope: Scope,
    *,
    key: str,
    request_digest: str,
    operation_id: str,
    now: datetime,
) -> Begun:
    if existing is not None:
        if existing.operation.request_digest != request_digest:
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
    return Begun(
        record=OperationRecord(tenant_id=tenant_id, account_id=account_id, operation=operation),
        fresh=True,
    )


def advance(
    record: OperationRecord,
    status: OperationStatus,
    *,
    now: datetime,
    resource: ResourceRef | None = None,
    result: Mapping[str, JsonValue] | None = None,
) -> OperationRecord:
    """Move to `status`. Repeating the current status is a no-op, so a
    replayed acknowledgement is harmless."""
    current = record.operation
    if status == current.status:
        return record
    if status not in TRANSITIONS[current.status]:
        raise InvalidTransition(current.key, current.status, status)
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


def recovery(operation: Operation) -> Recovery:
    """What a successor does with an operation it finds after a crash.

    `pending` was never sent, so it is safe to send. `sent` and
    `outcome_unknown` may have reached the provider: reconcile, never
    resend. `accepted` is known upstream: observe its outcome.
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
