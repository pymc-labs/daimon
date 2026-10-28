"""Repository-scoped durable entries. Callers own transactions and authorization."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from daimon.core._models import Account, RepoMemory, RepoMemorySession
from daimon.core.stores.github_installation_coverage import installation_covers_repo_sql
from mux.profiles import MANAGED_AGENTS
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


class MemoryEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    tenant_id: uuid.UUID
    repo_id: int
    name: str
    title: str
    body: str
    kind: Literal["reference", "directive"]
    scope: Literal["repo", "user"]
    created_by_account_id: uuid.UUID | None
    source_id: str | None = None
    created_at: datetime
    updated_at: datetime


async def list_entries(
    session: AsyncSession, *, tenant_id: uuid.UUID, repo_id: int, account_id: uuid.UUID | None
) -> list[MemoryEntry]:
    rows = await session.scalars(
        select(RepoMemory)
        .where(
            RepoMemory.tenant_id == tenant_id,
            RepoMemory.repo_id == repo_id,
            or_(RepoMemory.scope == "repo", RepoMemory.created_by_account_id == account_id)
            if account_id is not None
            else RepoMemory.scope == "repo",
        )
        .order_by(RepoMemory.name)
    )
    return [MemoryEntry.model_validate(row) for row in rows]


async def get_entry(
    session: AsyncSession, *, tenant_id: uuid.UUID, repo_id: int, name: str
) -> MemoryEntry | None:
    row = await session.get(RepoMemory, (tenant_id, repo_id, name))
    return MemoryEntry.model_validate(row) if row else None


async def _lock_slot(session: AsyncSession, tenant_id: uuid.UUID, repo_id: int, name: str) -> None:
    # Also serialize absent rows: SELECT FOR UPDATE alone cannot protect a first save.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:slot, 0))"),
        {"slot": f"repo-memory:{tenant_id}:{repo_id}:{name}"},
    )


async def save_entry(session: AsyncSession, *, entry: MemoryEntry) -> str | None:
    """Replace a visible slot, keeping its scope. Return the displaced title.

    Ownership checks run under the same transaction lock as the mutation, so a
    simultaneous first personal save cannot be overwritten by another person.
    """
    if entry.created_by_account_id is None:
        raise ValueError("Saving requires an authenticated creator.")
    await _lock_slot(session, entry.tenant_id, entry.repo_id, entry.name)
    row = await session.get(
        RepoMemory, (entry.tenant_id, entry.repo_id, entry.name), populate_existing=True
    )
    previous = row.title if row else None
    if row is not None:
        if row.scope == "user" and row.created_by_account_id != entry.created_by_account_id:
            raise ValueError("That memory name is unavailable; pick a distinct title.")
        if row.scope != entry.scope:
            raise ValueError("A save cannot change an entry's scope; use another title.")
        row.title = entry.title
        row.body = entry.body
        row.kind = entry.kind
        row.created_by_account_id = entry.created_by_account_id
        row.updated_at = entry.updated_at
    else:
        session.add(RepoMemory(**entry.model_dump()))
    await session.flush()
    return previous


async def delete_entry(
    session: AsyncSession, *, tenant_id: uuid.UUID, repo_id: int, name: str, account_id: uuid.UUID
) -> bool:
    await _lock_slot(session, tenant_id, repo_id, name)
    deleted = await session.scalar(
        delete(RepoMemory)
        .where(
            RepoMemory.tenant_id == tenant_id,
            RepoMemory.repo_id == repo_id,
            RepoMemory.name == name,
            or_(RepoMemory.scope == "repo", RepoMemory.created_by_account_id == account_id),
        )
        .returning(RepoMemory.name)
    )
    return deleted is not None


async def turn_entries(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    session_id: str | None = None,
    session_provider: str = MANAGED_AGENTS.provider,
) -> tuple[str, list[tuple[str, str, str, str, str]], str | None]:
    """One indexed query: live working grant, scoped confirmation and bounded catalog.

    Primary-key prefixes select the agent's grants and the repo's memory slots.
    References never transfer bodies. At most 512 rows and 12,001 characters per
    directive cross the DB boundary. There is no per-entry or network lookup.
    Agent-specific confirmations shadow shared ones even when revoked. The same
    statement reads the last delivered hash by tenant/provider/session primary key, including
    when no working grant remains, so revocation can clear a previous state block.
    """
    coverage = installation_covers_repo_sql("r.repo_full_name", "i")
    rows = (
        await session.execute(
            text(f"""
        WITH working AS (
            SELECT g.repo_id, r.repo_full_name
            FROM agent_github_grants g
            JOIN LATERAL (
                SELECT t.* FROM tenant_github_repos t
                WHERE t.tenant_id = g.tenant_id AND t.repo_id = g.repo_id
                  AND (t.scope_agent_id IS NULL OR t.scope_agent_id = g.agent_id)
                ORDER BY (t.scope_agent_id IS NULL) LIMIT 1
            ) r ON true
            JOIN github_app_installations i ON i.installation_id = r.installation_id
            WHERE g.tenant_id = :tenant_id AND g.agent_id = :agent_id
              AND g.is_working_repo AND g.app_visible AND r.status = 'active'
              AND {coverage}
            ORDER BY g.repo_id LIMIT 1
        )
        SELECT w.repo_full_name, m.name, m.title, m.kind, m.scope, m.body,
               (SELECT sha256 FROM repo_memory_sessions
                WHERE tenant_id = :tenant_id AND provider = :session_provider
                  AND session_id = :session_id) AS previous_sha256
        FROM (SELECT 1) anchor LEFT JOIN working w ON true LEFT JOIN LATERAL (
            WITH candidates AS (
                SELECT name, left(title, 1001) AS title, kind, scope,
                       CASE WHEN kind = 'directive' THEN left(body, 12001) ELSE '' END AS body
                FROM repo_memory
                WHERE tenant_id = :tenant_id AND repo_id = w.repo_id
                  AND (scope = 'repo' OR created_by_account_id = :account_id)
                ORDER BY kind, name LIMIT 512
            ), sized AS (
                SELECT *, sum(octet_length(title) + octet_length(body) + 256)
                    OVER (ORDER BY kind, name) AS bytes FROM candidates
            )
            SELECT name, title, kind, scope, body FROM sized WHERE bytes <= 40000
            UNION ALL
            SELECT '_more', 'Additional memories omitted; call repo_memory_list',
                   'reference', 'repo', ''
            WHERE EXISTS (SELECT 1 FROM sized WHERE bytes > 40000)
               OR (SELECT count(*) FROM sized) = 512
        ) m ON true
        ORDER BY m.kind, m.name
    """),
            {
                "tenant_id": tenant_id,
                "agent_id": agent_id,
                "account_id": account_id,
                "session_provider": session_provider,
                "session_id": session_id,
            },
        )
    ).all()
    return (
        str(rows[0][0] or ""),
        [
            (str(row[1]), str(row[2]), str(row[3]), str(row[4]), str(row[5]))
            for row in rows
            if row[1] is not None
        ],
        rows[0][6],
    )


async def import_entry(session: AsyncSession, *, entry: MemoryEntry) -> bool:
    """Insert an exported row exactly once; never overwrite destination changes."""
    account = (
        await session.get(Account, entry.created_by_account_id)
        if entry.created_by_account_id is not None
        else None
    )
    if account is None or account.tenant_id != entry.tenant_id:
        raise ValueError("Imported creator is missing or belongs to a different tenant.")
    await _lock_slot(session, entry.tenant_id, entry.repo_id, entry.name)
    existing = await get_entry(
        session, tenant_id=entry.tenant_id, repo_id=entry.repo_id, name=entry.name
    )
    if existing is not None:
        if existing != entry:
            raise ValueError("Destination memory conflicts with the export; nothing was imported.")
        return False
    session.add(RepoMemory(**entry.model_dump()))
    await session.flush()
    return True


async def record_turn_state(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_id: str,
    session_provider: str,
    sha256: str,
) -> None:
    """Record only a successful delivery, scoped to this provider's exact session."""
    await session.execute(
        insert(RepoMemorySession)
        .values(
            tenant_id=tenant_id,
            provider=session_provider,
            session_id=session_id,
            sha256=sha256,
        )
        .on_conflict_do_update(
            index_elements=[
                RepoMemorySession.tenant_id,
                RepoMemorySession.provider,
                RepoMemorySession.session_id,
            ],
            set_={"sha256": sha256},
        )
    )
