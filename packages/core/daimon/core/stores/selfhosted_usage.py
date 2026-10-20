"""Host billing admission and receipt context in the durable operation store.

These are local accounting operations, not provider send operations. A frozen
admission is committed before model I/O. The first receipt association retains
that snapshot across corrections without modifying the native usage DTO.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from daimon.core._models import JournalSession, MuxOperation, ProviderBinding
from mux.contracts.ids import ResourceRef
from mux.contracts.receipts import Operation
from mux.errors import ScopeViolation
from mux.state.operations import OperationRecord
from pydantic import JsonValue
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


def context_key(kind: str, binding_id: str, identity: str) -> str:
    digest = hashlib.sha256(json.dumps([binding_id, identity]).encode()).hexdigest()
    return f"selfhosted-billing:{kind}:{digest}"


def context_account(binding_id: str) -> str:
    return f"selfhosted-billing:{binding_id}"


async def check_session(
    session: AsyncSession, *, tenant_id: uuid.UUID, binding_id: str, native: ResourceRef
) -> None:
    if (
        native.kind != "session"
        or native.provider != "selfhosted"
        or native.tenant_id != str(tenant_id)
    ):
        raise ScopeViolation(native.id, "billing requires the authorized selfhosted session")
    provider_binding = select(ProviderBinding.binding_id).where(
        ProviderBinding.tenant_id == tenant_id,
        ProviderBinding.provider == "selfhosted",
        ProviderBinding.binding["native_refs"]["session"].astext == native.id,
    )
    owner = await session.scalar(
        select(JournalSession.session_id).where(
            JournalSession.tenant_id == tenant_id,
            JournalSession.binding_id == binding_id,
            JournalSession.session_id == native.id,
            JournalSession.binding_id.in_(provider_binding),
        )
    )
    if owner is None:
        raise ScopeViolation(native.id, "billing session differs from its tenant binding")


async def read_context(
    session: AsyncSession, *, tenant_id: uuid.UUID, binding_id: str, key: str, lock: bool = False
) -> dict[str, JsonValue] | None:
    query = select(MuxOperation.record).where(
        MuxOperation.tenant_id == tenant_id,
        MuxOperation.account_id == context_account(binding_id),
        MuxOperation.key == key,
    )
    body = await session.scalar(query.with_for_update() if lock else query)
    if body is None:
        return None
    return dict(OperationRecord.model_validate(body).result)


async def create_context(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    binding_id: str,
    key: str,
    value: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """First writer wins; frozen admission/receipt identity is never replaced."""
    now = datetime.now(UTC)
    record = OperationRecord(
        tenant_id=str(tenant_id),
        account_id=context_account(binding_id),
        principal_id="host:selfhosted-billing",
        operation=Operation(
            id=key, key=key, request_digest=key, status="processed", created_at=now, updated_at=now
        ),
        result=value,
    )
    await session.execute(
        insert(MuxOperation)
        .values(
            tenant_id=tenant_id,
            account_id=record.account_id,
            key=key,
            principal_id=record.principal_id,
            status="processed",
            record=record.model_dump(mode="json"),
            updated_at=now,
        )
        .on_conflict_do_nothing()
    )
    result = await read_context(
        session, tenant_id=tenant_id, binding_id=binding_id, key=key, lock=True
    )
    assert result is not None
    return result


async def note_pending_reason(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    binding_id: str,
    key: str,
    revision: int,
    reason: str | None,
) -> None:
    """Update operational reasons only; admission and receipt snapshot stay immutable."""
    query = select(MuxOperation.record).where(
        MuxOperation.tenant_id == tenant_id,
        MuxOperation.account_id == context_account(binding_id),
        MuxOperation.key == key,
    )
    body = await session.scalar(query.with_for_update())
    if body is None:
        raise ValueError("receipt context must be saved before its pending reason")
    try:
        record = OperationRecord.model_validate(body)
    except ValueError:
        now = datetime.now(UTC)
        record = OperationRecord(
            tenant_id=str(tenant_id),
            account_id=context_account(binding_id),
            principal_id="host:selfhosted-billing",
            operation=Operation(
                id=key,
                key=key,
                request_digest=key,
                status="processed",
                created_at=now,
                updated_at=now,
            ),
            result={"invalid_context": body},
        )
    prior = record.result.get("pending_reasons")
    reasons: dict[str, JsonValue] = dict(prior) if isinstance(prior, dict) else {}
    reasons[str(revision)] = reason
    result: dict[str, JsonValue] = {**record.result, "pending_reasons": reasons}
    record = record.model_copy(update={"result": result})
    await session.execute(
        update(MuxOperation)
        .where(
            MuxOperation.tenant_id == tenant_id,
            MuxOperation.account_id == context_account(binding_id),
            MuxOperation.key == key,
        )
        .values(record=record.model_dump(mode="json"))
    )
