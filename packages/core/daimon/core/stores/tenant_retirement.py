"""Retire a tenant: delete its data, keep its accounting, queue its upstream cleanup.

Retirement runs as steps, each its own transaction (`daimon.core.tenant_retirement`):
`begin_retirement`, `purge_batch` per table until empty, then `finish_retirement`.
Interrupted, the tenant stays archived and the next sweep resumes it.

Tenant tables come from `Base.metadata` (a `tenant_id` column or a foreign key to
`tenants`), so a new table is purged unless `KEEP_FOR_ACCOUNTING` names it with a
reason. Kept rows lose the person identifiers `PERSON_COLUMNS` lists. Provider
resources and hosted artifacts the deleted rows named are queued in
`tenant_retirements` first; `daimon.core.tenant_retirement` works the queue off.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from daimon.core._models import (
    Account,
    AccountGitHubLink,
    AgentChannelMemoryStore,
    AgentMemoryStore,
    AgentPersonalMemoryStore,
    Base,
    GitHubAppSessionVault,
    NeutralSkillVersion,
    SeededSkill,
    ServerSkill,
    SessionSettleState,
    Tenant,
    TenantRetirement,
    UsageEvent,
    UserSkill,
)
from daimon.core.stores import github_links as github_links_store
from daimon.core.stores import mux_erasure
from daimon.core.stores.security_audit import erase_tenant
from daimon.core.stores.slack_user_tokens import delete_workspace_tokens
from daimon.core.stores.tenants import delete_settle_state, tenant_session_ids
from sqlalchemy import (
    Column,
    Select,
    Table,
    delete,
    exists,
    func,
    literal,
    literal_column,
    not_,
    null,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import QueryableAttribute
from sqlalchemy.sql.elements import ColumnClause

# What a retired tenant keeps, and why. Everything else tenant-scoped is deleted.
KEEP_FOR_ACCOUNTING: dict[str, str] = {
    "tenant_ledger": "the balance, the trial and every credit or charge behind it",
    "payment_events": "payments received, which ledger credits point at",
    "usage_events": "billed usage, the evidence behind every charge",
    "promo_redemptions": "codes redeemed, so a code's redemption cap still counts them",
    "promo_channel_redemptions": "per-channel redemptions behind a code's channel cap",
    "usage_observation": "provider usage a charge was settled from",
    "accounting_outbox": "settlement records replayed when usage is reconciled",
    "accounting_binding_context": "what settling an outbox row reads once its slot is gone",
    "tenant_retirements": "the retirement's own queue of upstream cleanup",
}

# Person identifiers in kept rows, cleared on retirement.
PERSON_COLUMNS: dict[str, tuple[str, ...]] = {
    "tenant_ledger": ("actor_account_id",),
    "usage_events": ("platform_user_id", "managed_session_id"),
    "promo_redemptions": ("redeemed_by_account_id",),
    "promo_channel_redemptions": ("redeemed_by_account_id",),
    "accounting_outbox": ("row",),
}

# Purged through their own helper rather than a plain delete.
_OWN_HELPER = {"security_audit_events"}

PENDING_KINDS = ("sessions", "memory_stores", "vaults", "skills", "hosts")
HOSTS = ("reports", "notebooks")


def is_tenant_scoped(table: Table) -> bool:
    return "tenant_id" in table.c or any(
        fk.column.table.name == "tenants" for fk in table.foreign_keys
    )


def tenant_tables() -> list[Table]:
    """Every tenant-scoped table, children before parents."""
    return [t for t in reversed(Base.metadata.sorted_tables) if is_tenant_scoped(t)]


def _owned(
    column: QueryableAttribute[str | None],
    tenant: QueryableAttribute[uuid.UUID],
    tenant_id: uuid.UUID,
) -> Select[tuple[str | None]]:
    """Ids this tenant's rows name and no other tenant's row does."""
    shared = select(column).where(tenant != tenant_id)
    return select(column).where(tenant == tenant_id, column.is_not(None), column.not_in(shared))


async def collect_upstream(session: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, list[str]]:
    """Provider resources the tenant's rows name, read before those rows go."""
    ids = tenant_session_ids(tenant_id).subquery()
    sources: dict[str, list[Any]] = {
        "sessions": [select(ids.c[0]).where(ids.c[0].is_not(None))],
        "memory_stores": [
            _owned(m.memory_store_id, m.tenant_id, tenant_id)
            for m in (AgentMemoryStore, AgentChannelMemoryStore, AgentPersonalMemoryStore)
        ],
        "vaults": [
            _owned(GitHubAppSessionVault.vault_id, GitHubAppSessionVault.tenant_id, tenant_id)
        ],
        "skills": [
            _owned(UserSkill.anthropic_id, UserSkill.tenant_id, tenant_id),
            _owned(SeededSkill.anthropic_id, SeededSkill.tenant_id, tenant_id),
            _owned(ServerSkill.skill_id, ServerSkill.tenant_id, tenant_id),
            _owned(NeutralSkillVersion.skill_id, NeutralSkillVersion.tenant_id, tenant_id),
        ],
    }
    found: dict[str, list[str]] = {}
    for kind, queries in sources.items():
        values: set[str] = set()
        for query in queries:
            values.update(str(v) for v in await session.scalars(query))
        found[kind] = sorted(values)
    found["hosts"] = list(HOSTS)
    return found


async def _queue(session: AsyncSession, tenant_id: uuid.UUID, work: dict[str, list[str]]) -> None:
    current = await session.scalar(
        select(TenantRetirement.pending).where(TenantRetirement.tenant_id == tenant_id)
    )
    merged = {
        k: sorted(set(work.get(k, [])) | set((current or {}).get(k, []))) for k in PENDING_KINDS
    }
    await session.execute(
        insert(TenantRetirement)
        .values(tenant_id=tenant_id, pending=merged)
        .on_conflict_do_update(
            index_elements=[TenantRetirement.tenant_id], set_={"pending": merged}
        )
    )


async def _scrub(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    md = Base.metadata.tables
    for name, columns in PERSON_COLUMNS.items():
        table = md[name]
        values: dict[str, Any] = {}
        for column in columns:
            if column == "managed_session_id":
                # NOT NULL and part of a unique key: a stable pseudonym replaces it.
                col = cast(Column[str], table.c[column])
                values[column] = literal("retired:") + func.md5(col)
            elif column == "row":
                values[column] = func.jsonb_set(
                    table.c.row, text("'{observation,thread_id}'"), text("'null'::jsonb")
                )
            else:
                values[column] = null()
        stmt = update(table).where(table.c.tenant_id == tenant_id).values(**values)
        if name == "usage_events":
            stmt = stmt.where(not_(UsageEvent.managed_session_id.startswith("retired:")))
        await session.execute(stmt)


async def begin_retirement(
    session: AsyncSession, *, tenant_id: uuid.UUID, archived_before: datetime | None = None
) -> datetime | None:
    """Start retiring a tenant: archive it, queue its upstream cleanup, and drop the
    small, shared-key and settlement rows that cannot be purged by tenant in batches.

    Returns the tenant's archive time, which later steps must find unchanged (a reinstall
    restarts or clears it). None when the tenant does not exist, or, with `archived_before`
    (the sweep), when it is not archived since before that time, is already retired, or
    still has usage waiting to settle (retried on the next sweep). Safe to run again
    after an interruption.
    """
    if archived_before is not None and archived_before.utcoffset() is None:
        raise ValueError("archived_before must include a timezone")
    tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    if tenant is None:
        return None
    if archived_before is not None and (
        tenant.retired_at is not None
        or tenant.archived_at is None
        or tenant.archived_at >= archived_before
        or await has_pending_settle(session, tenant_id=tenant_id)
    ):
        return None
    # Archived first: a reinstall in the middle restarts or clears it, which stops the purge.
    archived_at = tenant.archived_at = tenant.archived_at or datetime.now(UTC)
    await _queue(session, tenant_id, await collect_upstream(session, tenant_id=tenant_id))
    links = await session.scalars(
        select(AccountGitHubLink.github_user_id)
        .join(Account, Account.id == AccountGitHubLink.account_id)
        .where(Account.tenant_id == tenant_id)
    )
    github_user_ids = set(links)
    if tenant.platform == "slack":
        # Keyed by Slack team, not tenant, so no tenant delete reaches them.
        await delete_workspace_tokens(session, tenant_id=tenant_id, team_id=tenant.external_id)
    await mux_erasure.detach_tenant_accounting(session, tenant_id=tenant_id)
    await delete_settle_state(session, tenant_id=tenant_id)
    await erase_tenant(session, tenant_id=tenant_id)
    await session.execute(
        delete(AccountGitHubLink).where(
            AccountGitHubLink.account_id.in_(
                select(Account.id).where(Account.tenant_id == tenant_id)
            )
        )
    )
    for github_user_id in github_user_ids:
        await github_links_store.delete_unlinked_user(session, github_user_id=github_user_id)
    return archived_at


def purge_tables() -> list[Table]:
    """The tables `purge_batch` empties, children before parents."""
    return [
        t
        for t in tenant_tables()
        if t.name not in KEEP_FOR_ACCOUNTING and t.name not in _OWN_HELPER
    ]


async def _still_retiring(
    session: AsyncSession, tenant_id: uuid.UUID, archived_at: datetime
) -> bool:
    """Lock the tenant row; false once a reinstall moved or cleared the archive time."""
    row = (
        await session.execute(
            select(Tenant.archived_at, Tenant.retired_at)
            .where(Tenant.id == tenant_id)
            .with_for_update()
        )
    ).first()
    return row is not None and row.archived_at == archived_at and row.retired_at is None


async def purge_batch(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    archived_at: datetime,
    table: Table,
    limit: int,
) -> int | None:
    """Delete up to `limit` of a retiring tenant's rows from `table`; None if the
    retirement was stopped by a reinstall, so the caller leaves the new rows alone."""
    if not await _still_retiring(session, tenant_id, archived_at):
        return None
    ctid: ColumnClause[Any] = literal_column("ctid")
    batch = select(ctid).select_from(table).where(table.c.tenant_id == tenant_id).limit(limit)
    result = await session.execute(delete(table).where(ctid.in_(batch)))
    return cast(CursorResult[Any], result).rowcount


async def finish_retirement(
    session: AsyncSession, *, tenant_id: uuid.UUID, archived_at: datetime
) -> bool:
    """Scrub the person identifiers kept rows still hold and mark the tenant retired."""
    if not await _still_retiring(session, tenant_id, archived_at):
        return False
    await _scrub(session, tenant_id)
    await session.execute(
        update(Tenant).where(Tenant.id == tenant_id).values(retired_at=func.now())
    )
    return True


async def has_pending_settle(session: AsyncSession, *, tenant_id: uuid.UUID) -> bool:
    """Whether any of the tenant's provider sessions still has usage waiting to settle."""
    ids = tenant_session_ids(tenant_id).subquery()
    return bool(
        await session.scalar(
            select(
                exists().where(
                    SessionSettleState.managed_session_id.in_(select(ids)),
                    SessionSettleState.settle_requested_at.is_not(None),
                )
            )
        )
    )


async def list_work(session: AsyncSession) -> list[tuple[uuid.UUID, dict[str, list[str]]]]:
    rows = await session.execute(select(TenantRetirement.tenant_id, TenantRetirement.pending))
    return [(tenant_id, pending) for tenant_id, pending in rows.tuples()]


async def get_work(session: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, list[str]] | None:
    return await session.scalar(
        select(TenantRetirement.pending).where(TenantRetirement.tenant_id == tenant_id)
    )


async def remove_done(
    session: AsyncSession, *, tenant_id: uuid.UUID, kind: str, done: set[str]
) -> bool:
    """Drop finished items; delete the work row once nothing is left. True when finished."""
    row = await session.scalar(
        select(TenantRetirement).where(TenantRetirement.tenant_id == tenant_id).with_for_update()
    )
    if row is None:
        return True
    pending = dict(row.pending)
    pending[kind] = [item for item in pending.get(kind, []) if item not in done]
    if not any(pending.get(k) for k in PENDING_KINDS):
        await session.delete(row)
        return True
    row.pending = pending
    return False
