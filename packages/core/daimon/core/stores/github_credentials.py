"""Async store for github_credentials.

UPSERT on principal_id: re-OAuth replaces the row with fresh
github_login + encrypted_token + scopes and bumps updated_at. No
try/except — DB exceptions propagate to the route boundary in Plan 05.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from daimon.core._models import GitHubCredential
from daimon.core.stores.domain import GitHubCredentialRow
from sqlalchemy import CursorResult, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_credential(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    github_login: str,
    encrypted_token: bytes,
    scopes: tuple[str, ...],
) -> GitHubCredentialRow:
    now = datetime.now(tz=UTC)
    stmt = pg_insert(GitHubCredential).values(
        principal_id=principal_id,
        github_login=github_login,
        encrypted_token=encrypted_token,
        scopes=list(scopes),
        created_at=now,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[GitHubCredential.principal_id],
        set_={
            "github_login": github_login,
            "encrypted_token": encrypted_token,
            "scopes": list(scopes),
            "updated_at": now,
        },
    ).returning(GitHubCredential)

    result = await session.execute(stmt)
    orm = result.scalar_one()
    return GitHubCredentialRow.model_validate(orm)


async def get_credential_by_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> GitHubCredentialRow | None:
    orm = await session.get(GitHubCredential, principal_id)
    if orm is None:
        return None
    return GitHubCredentialRow.model_validate(orm)


async def get_credential_login_by_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> str | None:
    """Return ``github_login`` for the principal, or None if no credential.

    Used by /cli/auth/status — never returns the encrypted token. The
    intentionally narrow signature avoids fanning credential plaintext (or
    even ciphertext) out to the route layer (T-19-05-02).
    """
    orm = await session.get(GitHubCredential, principal_id)
    if orm is None:
        return None
    return str(orm.github_login)


async def delete_credential_for_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> int:
    """Delete the github_credentials row for a principal. Idempotent.

    Returns rowcount; never raises on 0. Used by the GDPR purge orchestrator.

    Keyed by principal_id alone — the table has no tenant_id column.
    principal_id is the PK so rowcount is 0 or 1; the purge registry consumes
    ints uniformly.
    """
    result = await session.execute(
        delete(GitHubCredential).where(GitHubCredential.principal_id == principal_id)
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount
