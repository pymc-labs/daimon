"""Serialize a root's disjoint native usage before the accounting batch writes."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from daimon.core._models import AccountingOutbox, UsageObservation
from daimon.core.stores.accounting_outbox import lock_provider_billing
from mux.contracts.usage import UsageObservation as UsageDTO
from mux.state.usage_ledger import AppliedUsage, OutboxRow
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession


async def lock_root_usage(
    session: AsyncSession,
    binding_id: str,
    observations: Sequence[UsageDTO],
    *,
    tenant_id: uuid.UUID,
) -> tuple[OutboxRow, ...]:
    """Lock all observation IDs in order BEFORE the existing binding lock.

    Taking a binding lock and then a second observation lock can deadlock with
    the single-observation producer. This order also serializes competing roots.
    Check attribution even for stale snapshots, before stale-revision elision.
    """
    identities = {value.id: value for value in observations}
    for identity in sorted(identities):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"mux.usage\x1f{binding_id}\x1f{identity}"},
        )
    await lock_provider_billing(session, binding_id)
    bodies = await session.scalars(
        select(UsageObservation.applied)
        .where(
            UsageObservation.binding_id == binding_id,
            UsageObservation.tenant_id == tenant_id,
        )
        .order_by(UsageObservation.revision)
    )
    for body in bodies:
        prior = AppliedUsage.model_validate(body).observation
        root = observations[0]
        if (
            prior.session == root.session
            and prior.turn_id == root.turn_id
            and prior.id not in identities
        ):
            raise ValueError("root usage batch cannot omit previously recorded native work")
        if prior.id not in identities:
            continue
        current = identities[prior.id]
        if (
            prior.session != current.session
            or prior.turn_id != current.turn_id
            or prior.thread_id != current.thread_id
        ):
            raise ValueError("native usage must retain its original root attribution")
        if prior.revision > current.revision:
            raise ValueError("root usage requires latest durable revisions")
    settled = await session.scalars(
        select(AccountingOutbox.row).where(
            AccountingOutbox.binding_id == binding_id,
            AccountingOutbox.observation_id.in_(identities),
            AccountingOutbox.tenant_id == tenant_id,
            AccountingOutbox.applied_at.is_not(None),
        )
    )
    return tuple(OutboxRow.model_validate(body) for body in settled)
