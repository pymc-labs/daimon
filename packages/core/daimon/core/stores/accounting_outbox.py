"""Read the durable snapshots needed by the host's accounting transaction.

All neutral-state mutations use the public functions in stores.mux_state.
This store owns only the host's reads; it never commits or opens a session.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from daimon.core._models import AccountingOutbox, TenantLedger, UsageObservation
from daimon.core.stores import selfhosted_usage as billing_contexts
from daimon.core.usage_aggregation import disjoint_observations, replace_observation
from mux.contracts.usage import UsageObservation as UsageDTO
from mux.errors import ScopeViolation
from mux.profiles import accounting_mode_for
from mux.state.usage_ledger import AppliedUsage, OutboxRow
from pydantic import BaseModel
from sqlalchemy import func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class UsageRevisions:
    current: AppliedUsage
    prior: AppliedUsage | None
    occurred_at: datetime


async def lock_provider_billing(session: AsyncSession, binding_id: str) -> None:
    """Serialize grain selection after taking the producer's observation lock.

    Every applier retains that order, including a caller which recorded the
    observation in this same transaction before invoking apply_usage_outbox.
    Grain reads do not acquire other observations' advisory locks.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"daimon.provider_billing\x1f{binding_id}"},
    )


async def check_provider_ingest(
    session: AsyncSession,
    binding_id: str,
    observation: UsageDTO,
    *,
    tenant_id: uuid.UUID,
) -> None:
    """Refuse mutable or overlapping coverage before capture or settlement.

    Pending observations participate too. Lock observation then binding, just
    like the producer/applier composition; never take a sibling's lock.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"mux.usage\x1f{binding_id}\x1f{observation.id}"},
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
    latest: dict[str, UsageDTO] = {}
    for body in bodies:
        prior = AppliedUsage.model_validate(body).observation
        before = latest.get(prior.id)
        if before is not None and frozenset(before.covers) != frozenset(prior.covers):
            raise ValueError("usage corrections must retain their coverage set")
        latest[prior.id] = prior
    updated = replace_observation(tuple(latest.values()), observation)
    disjoint_observations(updated)


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
        or (
            prior.observation.model is not None
            and prior.observation.model != current.observation.model
        )
        or prior.observation.grain != current.observation.grain
        or prior.observation.basis != current.observation.basis
        or (
            accounting_mode_for(current.observation.session.provider).settles_natively
            and (
                prior.observation.turn_id != current.observation.turn_id
                or prior.observation.thread_id != current.observation.thread_id
            )
        )
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
    require_settled: bool = False,
) -> bool:
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
    if require_settled and any(identity not in latest for identity in row.observation.covers):
        return False  # keep the aggregate pending until the complete walk arrives
    if any(
        observation_id not in latest
        or latest[observation_id].observation.grain != billing_grain
        or latest[observation_id].observation.covers
        or latest[observation_id].observation.session != row.observation.session
        for observation_id in row.observation.covers
    ):
        raise ValueError("aggregate coverage requires durable observations at the billing grain")
    if require_settled:
        settled = set(
            await session.execute(
                select(AccountingOutbox.observation_id, AccountingOutbox.revision).where(
                    AccountingOutbox.binding_id == row.binding_id,
                    AccountingOutbox.observation_id.in_(row.observation.covers),
                    AccountingOutbox.tenant_id == tenant_id,
                    AccountingOutbox.applied_at.is_not(None),
                )
            )
        )
        return all(
            (identity, latest[identity].revision) in settled for identity in row.observation.covers
        )
    return True


async def pending_revision(
    session: AsyncSession, binding_id: str, observation_id: str, revision: int
) -> OutboxRow | None:
    """Retry settlement when actual infrastructure becomes known after capture."""
    body = await session.scalar(
        select(AccountingOutbox.row).where(
            AccountingOutbox.binding_id == binding_id,
            AccountingOutbox.observation_id == observation_id,
            AccountingOutbox.revision == revision,
            AccountingOutbox.applied_at.is_(None),
        )
    )
    return OutboxRow.model_validate(body) if body is not None else None


async def settled_amount(
    session: AsyncSession, row: OutboxRow, *, tenant_id: uuid.UUID, channel_id: str | None
) -> tuple[int, Decimal]:
    """Read actual prior debits under load_revisions' observation lock.

    Exact settlement replaces the last verified absolute cost. Pending unknown
    snapshots never count as already billed, and a late older snapshot cannot
    rewind a newer verified settlement.
    """
    revisions = tuple(
        await session.scalars(
            select(AccountingOutbox.revision).where(
                AccountingOutbox.binding_id == row.binding_id,
                AccountingOutbox.observation_id == row.observation_id,
                AccountingOutbox.tenant_id == tenant_id,
                AccountingOutbox.applied_at.is_not(None),
            )
        )
    )
    if not revisions:
        return 0, Decimal("0.000000")
    keys = [
        f"turn:{row.observation.session.id}:{row.observation_id}"
        if revision == 1
        else f"adjust:{row.binding_id}:{row.observation_id}:{revision}"
        for revision in revisions
    ]
    contexts = (
        await session.execute(
            select(TenantLedger.tenant_id, TenantLedger.channel_id).where(
                TenantLedger.idempotency_key.in_(keys)
            )
        )
    ).all()
    if any(context != (tenant_id, channel_id) for context in contexts):
        raise ScopeViolation(row.observation_id, "settled usage belongs to another billing context")
    amount = await session.scalar(
        select(-func.coalesce(func.sum(TenantLedger.delta_usd), 0)).where(
            TenantLedger.idempotency_key.in_(keys),
            TenantLedger.tenant_id == tenant_id,
            TenantLedger.channel_id == channel_id,
        )
    )
    return max(revisions), Decimal(amount or 0)


async def check_provider_grain(
    session: AsyncSession, row: OutboxRow, *, tenant_id: uuid.UUID
) -> None:
    """A session aggregate and its turn/request snapshots cannot both charge."""
    bodies = await session.scalars(
        select(AccountingOutbox.row).where(
            AccountingOutbox.binding_id == row.binding_id,
            AccountingOutbox.tenant_id == tenant_id,
            AccountingOutbox.applied_at.is_not(None),
        )
    )
    for body in bodies:
        prior = OutboxRow.model_validate(body).observation
        if (
            prior.session == row.observation.session
            and not prior.covers
            and prior.grain != row.observation.grain
        ):
            raise ValueError("overlapping provider settlements require one billing grain")


class PendingUsageRow(BaseModel):
    """Content-free billing gaps; bad bodies expose only their relational identity."""

    binding_id: str
    session_id: str | None
    observation_id: str
    revision: int
    model: str | None
    completeness: str
    observed_at: datetime
    reason: str


class PendingUsageCursor(BaseModel):
    tenant_id: uuid.UUID
    created_at: datetime
    binding_id: str
    observation_id: str

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def decode(cls, value: str) -> PendingUsageCursor:
        try:
            return cls.model_validate_json(base64.b64decode(value, altchars=b"-_", validate=True))
        except (ValueError, binascii.Error) as error:
            raise ValueError("invalid pending-usage cursor") from error


class PendingUsagePage(BaseModel):
    rows: list[PendingUsageRow]
    next_cursor: str | None
    truncated: bool
    invalid_rows: int = 0


async def list_pending_usage(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit: int = 100, cursor: str | None = None
) -> PendingUsagePage:
    """Oldest pending rows first, with tenant-bound keyset pagination.

    Superseded snapshots stay auditable but do not need operator action.
    Corrupt bodies become flagged rows instead of hiding the rest of the page.
    Native payloads and user content are never returned.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    after = PendingUsageCursor.decode(cursor) if cursor is not None else None
    if after is not None and after.tenant_id != tenant_id:
        raise ValueError("pending-usage cursor belongs to another tenant")
    latest = (
        select(
            AccountingOutbox.binding_id,
            AccountingOutbox.observation_id,
            func.max(AccountingOutbox.revision).label("revision"),
        )
        .where(AccountingOutbox.tenant_id == tenant_id)
        .group_by(AccountingOutbox.binding_id, AccountingOutbox.observation_id)
        .subquery()
    )
    query = (
        select(
            AccountingOutbox.binding_id,
            AccountingOutbox.observation_id,
            AccountingOutbox.revision,
            AccountingOutbox.created_at,
            AccountingOutbox.row,
        )
        .join(
            latest,
            (AccountingOutbox.binding_id == latest.c.binding_id)
            & (AccountingOutbox.observation_id == latest.c.observation_id)
            & (AccountingOutbox.revision == latest.c.revision),
        )
        .where(AccountingOutbox.tenant_id == tenant_id, AccountingOutbox.applied_at.is_(None))
        .order_by(
            AccountingOutbox.created_at,
            AccountingOutbox.binding_id,
            AccountingOutbox.observation_id,
        )
        .limit(limit + 1)
    )
    if after is not None:
        query = query.where(
            tuple_(
                AccountingOutbox.created_at,
                AccountingOutbox.binding_id,
                AccountingOutbox.observation_id,
            )
            > (after.created_at, after.binding_id, after.observation_id)
        )
    with session.no_autoflush:
        bodies = list((await session.execute(query)).all())
        rows: list[PendingUsageRow] = []
        invalid = 0
        for binding_id, identity, revision, created_at, body in bodies[:limit]:
            try:
                row = OutboxRow.model_validate(body)
                value = row.observation
                if (
                    row.key != (binding_id, identity, revision)
                    or (value.id, value.revision) != (identity, revision)
                    or value.session.tenant_id
                    not in (
                        None,
                        str(tenant_id),
                    )
                ):
                    raise ValueError("pending body identity differs from its relational owner")
                reason = (
                    f"usage_{value.completeness}"
                    if value.completeness != "measured"
                    else "reconciliation_required"
                )
                try:
                    context = await billing_contexts.read_context(
                        session,
                        tenant_id=tenant_id,
                        binding_id=binding_id,
                        key=billing_contexts.context_key("receipt", binding_id, identity),
                    )
                    reasons = context.get("pending_reasons") if context else None
                    saved = reasons.get(str(revision)) if isinstance(reasons, dict) else None
                    if isinstance(saved, str):
                        reason = saved
                except ValueError:
                    reason = "tariff_context_invalid"
                rows.append(
                    PendingUsageRow(
                        binding_id=binding_id,
                        session_id=value.session.id,
                        observation_id=identity,
                        revision=revision,
                        model=value.model.id if value.model else None,
                        completeness=value.completeness,
                        observed_at=value.observed_at,
                        reason=reason,
                    )
                )
            except ValueError:
                invalid += 1
                rows.append(
                    PendingUsageRow(
                        binding_id=binding_id,
                        session_id=None,
                        observation_id=identity,
                        revision=revision,
                        model=None,
                        completeness="unknown",
                        observed_at=created_at,
                        reason="invalid_observation",
                    )
                )
        truncated = len(bodies) > limit
        next_cursor = None
        if truncated:
            binding_id, identity, _, created_at, _ = bodies[limit - 1]
            next_cursor = PendingUsageCursor(
                tenant_id=tenant_id,
                created_at=created_at,
                binding_id=binding_id,
                observation_id=identity,
            ).encode()
        return PendingUsagePage(
            rows=rows,
            next_cursor=next_cursor,
            truncated=truncated,
            invalid_rows=invalid,
        )
