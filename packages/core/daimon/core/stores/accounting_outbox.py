"""Read the durable snapshots needed by the host's accounting transaction.

All neutral-state mutations use the public functions in stores.mux_state.
This store owns only the host's reads; it never commits or opens a session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from daimon.core._models import AccountingOutbox, TenantLedger, UsageObservation
from mux.errors import ScopeViolation
from mux.state.usage_ledger import AppliedUsage, OutboxRow
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class UsageRevisions:
    current: AppliedUsage
    prior: AppliedUsage | None
    occurred_at: datetime


async def _revision(
    session: AsyncSession, row: OutboxRow, revision: int, tenant_id: uuid.UUID
) -> AppliedUsage:
    body = await session.scalar(
        select(UsageObservation.applied).where(
            UsageObservation.binding_id == row.binding_id,
            UsageObservation.observation_id == row.observation_id,
            UsageObservation.revision == revision,
            UsageObservation.tenant_id == tenant_id,
        )
    )
    if body is None:
        raise ValueError("accounting requires the durable usage revision")
    return AppliedUsage.model_validate(body)


async def load_revisions(
    session: AsyncSession, row: OutboxRow, *, tenant_id: uuid.UUID
) -> UsageRevisions:
    """Serialize one observation and read its exact current/prior snapshots."""
    # Match the producer's lock order/key, including concurrent revision writes.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"mux.usage\x1f{row.binding_id}\x1f{row.observation_id}"},
    )
    stored = (
        await session.execute(
            select(AccountingOutbox.tenant_id, AccountingOutbox.row).where(
                AccountingOutbox.binding_id == row.binding_id,
                AccountingOutbox.observation_id == row.observation_id,
                AccountingOutbox.revision == row.revision,
            )
        )
    ).one_or_none()
    if stored is None:
        raise KeyError(f"no outbox row {row.key}")
    owner, body = stored
    if owner != tenant_id:
        raise ScopeViolation(row.observation_id, "accounting row belongs to another tenant")
    canonical = OutboxRow.model_validate(body)
    if canonical != row.model_copy(update={"applied": False}):
        raise ValueError("accounting requires the durable outbox contents")
    current = await _revision(session, row, row.revision, tenant_id)
    prior = (
        await _revision(session, row, row.prior_applied_revision, tenant_id)
        if row.prior_applied_revision is not None
        else None
    )
    if current.observation != canonical.observation:
        raise ValueError("outbox and usage revision disagree")
    if prior is not None and (
        prior.observation.session != current.observation.session
        or prior.observation.model != current.observation.model
        or prior.observation.grain != current.observation.grain
        or prior.observation.basis != current.observation.basis
    ):
        raise ValueError("a usage correction must preserve its billing identity")
    first_body = await session.scalar(
        select(UsageObservation.applied)
        .where(
            UsageObservation.binding_id == row.binding_id,
            UsageObservation.observation_id == row.observation_id,
            UsageObservation.tenant_id == tenant_id,
        )
        .order_by(UsageObservation.revision)
        .limit(1)
    )
    assert first_body is not None
    return UsageRevisions(
        current=current,
        prior=prior,
        occurred_at=AppliedUsage.model_validate(first_body).observation.observed_at,
    )


async def check_ledger_key(
    session: AsyncSession,
    *,
    key: str,
    tenant_id: uuid.UUID,
    channel_id: str | None,
    allow_legacy: bool,
) -> None:
    """Only a matching historical turn may already own an unapplied row's key."""
    existing = (
        await session.execute(
            select(TenantLedger.tenant_id, TenantLedger.channel_id).where(
                TenantLedger.idempotency_key == key
            )
        )
    ).one_or_none()
    if existing is None:
        return
    if existing != (tenant_id, channel_id):
        raise ScopeViolation(key, "ledger key belongs to another billing context")
    if not allow_legacy:
        raise ValueError("an unapplied correction already has a ledger entry")


async def check_covered_usage(
    session: AsyncSession,
    row: OutboxRow,
    *,
    tenant_id: uuid.UUID,
    billing_grain: str,
) -> None:
    """An aggregate is redundant only when every covered billing leaf is durable."""
    bodies = (
        await session.scalars(
            select(UsageObservation.applied)
            .where(
                UsageObservation.binding_id == row.binding_id,
                UsageObservation.observation_id.in_(row.observation.covers),
                UsageObservation.tenant_id == tenant_id,
            )
            .order_by(UsageObservation.revision)
        )
    ).all()
    latest = {
        applied.observation_id: applied for applied in map(AppliedUsage.model_validate, bodies)
    }
    if any(
        observation_id not in latest
        or latest[observation_id].observation.grain != billing_grain
        or latest[observation_id].observation.covers
        or latest[observation_id].observation.session != row.observation.session
        for observation_id in row.observation.covers
    ):
        raise ValueError("aggregate coverage requires durable observations at the billing grain")
