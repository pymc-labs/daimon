"""Per-agent environment values, encrypted at rest with deployment MultiFernet keys."""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from cryptography.fernet import MultiFernet
from daimon.core._models import AgentFile
from daimon.core.agent_env_crypto import PREFIX, decode_value, encode_value
from daimon.core.config import load_crypto_settings
from daimon.core.errors import StoreError
from daimon.core.github_credentials import build_multifernet
from daimon.core.stores.domain import AgentFileRow
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


def _cipher(session: AsyncSession) -> MultiFernet | None:
    """Use injected deployment keys; raw sessions resolve the same crypto settings."""
    keys = session.info.get(
        "crypto_keys", session.get_bind().get_execution_options().get("crypto_keys")
    )
    if keys is None:
        keys = tuple(k.get_secret_value() for k in load_crypto_settings().keys)
    return build_multifernet(keys) if keys else None


def _row(cipher: MultiFernet | None, orm: AgentFile) -> AgentFileRow:
    if cipher is not None and not orm.content.startswith(PREFIX):
        structlog.get_logger(__name__).warning(
            "agent_env.legacy_plaintext",
            tenant_id=str(orm.tenant_id),
            agent_id=str(orm.agent_id),
            key=orm.key,
        )
    return AgentFileRow.model_validate(orm).model_copy(
        update={"content": decode_value(cipher, orm.content)}
    )


async def put_agent_file(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    key: str,
    content: str,
    set_by_account_id: uuid.UUID | None,
) -> AgentFileRow:
    """Upsert text content for (tenant_id, agent_id, key). Last-write-wins.

    Returns the post-write row. Uses `.returning(...)` + `scalar_one()` so the
    cursor — not the identity map — is the source of truth, mirroring
    `agent_repo_binding.set_binding`. This is the safe shape when the caller
    (or a downstream caller in the same session) reads this row again.

    `set_by_account_id` has no default, for the reason `set_binding`'s `proof`
    has none: every write must state who made it, and `None` said explicitly
    is the legitimate way to record a write with no acting person. It lands in
    `created_by_account_id` and `last_set_by_account_id` on insert, but the
    conflict path updates only the latter — the creator of a key survives
    every later replacement of its value.
    """
    if key == "":
        raise StoreError("key must not be empty")

    cipher = _cipher(session)
    content = encode_value(cipher, content)
    stmt = (
        pg_insert(AgentFile)
        .values(
            tenant_id=tenant_id,
            agent_id=agent_id,
            key=key,
            content=content,
            created_by_account_id=set_by_account_id,
            last_set_by_account_id=set_by_account_id,
        )
        .on_conflict_do_update(
            constraint="pk_agent_files",
            set_={
                "content": content,
                "updated_at": func.now(),
                "last_set_by_account_id": set_by_account_id,
            },
        )
        .returning(AgentFile)
        .execution_options(populate_existing=True)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one()
    await session.flush()
    return _row(cipher, orm)


async def put_agent_file_if_unchanged(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    key: str,
    content: str,
    set_by_account_id: uuid.UUID | None,
    expected_updated_at: datetime | None,
) -> AgentFileRow | None:
    """Write only if the key is still exactly as the posted card described it.

    `expected_updated_at is None` means the card promised the key did not
    exist, so the write is `INSERT ... ON CONFLICT DO NOTHING RETURNING` — a
    key that appeared in the meantime is a failed precondition. Otherwise the
    card promised a specific existing value, and the write is
    `UPDATE ... WHERE updated_at = :expected RETURNING`.

    Returns `None` for "precondition failed". The caller cannot — and must
    not — distinguish "someone else wrote" from "the key was removed": both
    mean the card's promise no longer holds and both get the same response.

    One statement either way. The row lock the statement takes is the entire
    gate, exactly like `consume_credential_request`: a concurrent writer's
    WHERE clause simply matches zero rows, so no read-then-write race exists
    and no advisory lock is needed.

    The precondition is transaction-granular, because `updated_at` is `now()`
    (transaction start time): two writes inside one transaction stamp the same
    value and the second would not be detected as a change. That is harmless —
    a real race is between transactions, and a caller that already holds the
    row in its own transaction is not racing itself.
    """
    if key == "":
        raise StoreError("key must not be empty")

    cipher = _cipher(session)
    content = encode_value(cipher, content)
    if expected_updated_at is None:
        insert_stmt = (
            pg_insert(AgentFile)
            .values(
                tenant_id=tenant_id,
                agent_id=agent_id,
                key=key,
                content=content,
                created_by_account_id=set_by_account_id,
                last_set_by_account_id=set_by_account_id,
            )
            .on_conflict_do_nothing(constraint="pk_agent_files")
            .returning(AgentFile)
        )
        result = await session.execute(insert_stmt)
    else:
        update_stmt = (
            update(AgentFile)
            .where(
                AgentFile.tenant_id == tenant_id,
                AgentFile.agent_id == agent_id,
                AgentFile.key == key,
                AgentFile.updated_at == expected_updated_at,
            )
            .values(
                content=content,
                updated_at=func.now(),
                last_set_by_account_id=set_by_account_id,
            )
            .returning(AgentFile)
        )
        result = await session.execute(update_stmt)
    orm = result.scalar_one_or_none()
    await session.flush()
    if orm is None:
        return None
    return _row(cipher, orm)


async def get_agent_file(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    key: str,
) -> AgentFileRow | None:
    """Return the row at (tenant_id, agent_id, key), or None if absent."""
    orm = await session.get(AgentFile, (tenant_id, agent_id, key))
    if orm is None:
        return None
    return _row(_cipher(session), orm)


async def list_agent_files(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> list[AgentFileRow]:
    """Return all files for this (tenant, agent) ordered by key."""
    result = await session.execute(
        select(AgentFile)
        .where(
            AgentFile.tenant_id == tenant_id,
            AgentFile.agent_id == agent_id,
        )
        .order_by(AgentFile.key)
    )
    rows = result.scalars().all()
    cipher = _cipher(session) if rows else None
    return [_row(cipher, o) for o in rows]


async def delete_agent_file(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    key: str,
) -> None:
    """Delete the row at (tenant_id, agent_id, key). Idempotent — no raise if absent."""
    await session.execute(
        delete(AgentFile).where(
            AgentFile.tenant_id == tenant_id,
            AgentFile.agent_id == agent_id,
            AgentFile.key == key,
        )
    )
    await session.flush()
