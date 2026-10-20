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

from daimon.core._models import (
    AccountingOutbox,
    JournalSession,
    MuxOperation,
    ProviderBinding,
    UsageObservation,
)
from mux.contracts.ids import ModelRef, ResourceRef
from mux.contracts.receipts import Operation
from mux.errors import ScopeViolation
from mux.state.operations import OperationRecord
from mux.state.usage_ledger import AppliedUsage, OutboxRow
from pydantic import JsonValue
from sqlalchemy import select, text, update
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


async def recorded_model(
    session: AsyncSession, *, tenant_id: uuid.UUID, binding_id: str, observation_id: str
) -> ModelRef | None:
    """Lock the observation and recover identity without consulting tariff context."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"mux.usage\x1f{binding_id}\x1f{observation_id}"},
    )
    body = await session.scalar(
        select(UsageObservation.applied)
        .where(
            UsageObservation.tenant_id == tenant_id,
            UsageObservation.binding_id == binding_id,
            UsageObservation.observation_id == observation_id,
        )
        .order_by(UsageObservation.revision.desc())
        .limit(1)
    )
    return AppliedUsage.model_validate(body).observation.model if body is not None else None


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


async def adopt_admission(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    binding_id: str,
    key: str,
    observation_id: str,
    admission: dict[str, JsonValue],
    deployment: str,
    model: ModelRef,
    earliest_observed_at: datetime,
) -> dict[str, JsonValue]:
    """Associate an unpriced receipt once, before any settlement.

    Only host accounting model aliases in unclaimed rows are normalised. Token
    counts, native meters and source aliases remain intact. The caller validates
    the existing admission against the receipt before entering this transition.
    """
    original = await read_context(
        session, tenant_id=tenant_id, binding_id=binding_id, key=key, lock=True
    )
    if original is None:
        raise ValueError("receipt context must exist before admission adoption")
    if original.get("admission") is not None or original.get("invalid_context") is not None:
        return original
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"mux.usage\x1f{binding_id}\x1f{observation_id}"},
    )
    # Match the accounting producer's lock order before examining its rows.
    from daimon.core.stores.accounting_outbox import lock_provider_billing

    await lock_provider_billing(session, binding_id)
    outbox = list(
        await session.scalars(
            select(AccountingOutbox).where(
                AccountingOutbox.tenant_id == tenant_id,
                AccountingOutbox.binding_id == binding_id,
                AccountingOutbox.observation_id == observation_id,
            )
        )
    )
    observations = list(
        await session.scalars(
            select(UsageObservation).where(
                UsageObservation.tenant_id == tenant_id,
                UsageObservation.binding_id == binding_id,
                UsageObservation.observation_id == observation_id,
            )
        )
    )
    saved_outbox = [OutboxRow.model_validate(row.row) for row in outbox]
    saved_usage = [AppliedUsage.model_validate(row.applied) for row in observations]
    if any(row.applied_at is not None for row in outbox) or any(
        value.observation.observed_at < earliest_observed_at
        or (
            value.observation.model is not None
            and value.observation.model
            not in (model, ModelRef(provider="selfhosted", id=deployment))
        )
        for value in [*saved_outbox, *saved_usage]
    ):
        raise ValueError("admission cannot replace a settled or unrelated billing identity")
    for row, value in zip(outbox, saved_outbox, strict=True):
        row.row = value.model_copy(
            update={"observation": value.observation.model_copy(update={"model": model})}
        ).model_dump(mode="json")
    for usage_row, usage in zip(observations, saved_usage, strict=True):
        usage_row.applied = usage.model_copy(
            update={"observation": usage.observation.model_copy(update={"model": model})}
        ).model_dump(mode="json")
    body = await session.scalar(
        select(MuxOperation.record).where(
            MuxOperation.tenant_id == tenant_id,
            MuxOperation.account_id == context_account(binding_id),
            MuxOperation.key == key,
        )
    )
    record = OperationRecord.model_validate(body)
    result: dict[str, JsonValue] = {**original, "admission": admission}
    await session.execute(
        update(MuxOperation)
        .where(
            MuxOperation.tenant_id == tenant_id,
            MuxOperation.account_id == context_account(binding_id),
            MuxOperation.key == key,
        )
        .values(record=record.model_copy(update={"result": result}).model_dump(mode="json"))
    )
    await session.flush()
    return result


async def note_pending_reason(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    binding_id: str,
    key: str,
    revision: int,
    reason: str | None,
    native_model: ModelRef | None,
) -> None:
    """Save revision status and source aliases without replacing the admitted tariff."""
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
    prior_models = record.result.get("native_models")
    models: dict[str, JsonValue] = dict(prior_models) if isinstance(prior_models, dict) else {}
    models.setdefault(
        str(revision), native_model.model_dump(mode="json") if native_model is not None else None
    )
    result: dict[str, JsonValue] = {
        **record.result,
        "pending_reasons": reasons,
        "native_models": models,
    }
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


async def replay_cursor(session: AsyncSession, *, tenant_id: uuid.UUID, binding_id: str) -> int:
    value = await read_context(
        session,
        tenant_id=tenant_id,
        binding_id=binding_id,
        key=context_key("replay", binding_id, "cursor"),
    )
    cursor = value.get("seq", 0) if value is not None else 0
    if not isinstance(cursor, int) or cursor < 0:
        raise ValueError("invalid self-hosted replay cursor")
    return cursor


async def advance_replay_cursor(
    session: AsyncSession, *, tenant_id: uuid.UUID, binding_id: str, seq: int
) -> None:
    """Advance only after every receipt through seq has been durably recorded."""
    key = context_key("replay", binding_id, "cursor")
    await create_context(
        session, tenant_id=tenant_id, binding_id=binding_id, key=key, value={"seq": 0}
    )
    current = await replay_cursor(session, tenant_id=tenant_id, binding_id=binding_id)
    if seq <= current:
        return
    body = await session.scalar(
        select(MuxOperation.record).where(
            MuxOperation.tenant_id == tenant_id,
            MuxOperation.account_id == context_account(binding_id),
            MuxOperation.key == key,
        )
    )
    record = OperationRecord.model_validate(body)
    await session.execute(
        update(MuxOperation)
        .where(
            MuxOperation.tenant_id == tenant_id,
            MuxOperation.account_id == context_account(binding_id),
            MuxOperation.key == key,
        )
        .values(record=record.model_copy(update={"result": {"seq": seq}}).model_dump(mode="json"))
    )
