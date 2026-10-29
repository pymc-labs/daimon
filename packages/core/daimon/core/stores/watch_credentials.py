"""Owner-supplied read keys for watch polls, one per (tenant, owner, server URL).

Rows hold ciphertext only; callers encrypt and decrypt with the deployment's
MultiFernet. A key is always looked up by the watch's own tenant and owner, so
one owner's watch can never load another owner's key. URLs are stored in
their canonical ASCII form and matched exactly. No try/except: DB errors
propagate to the caller's boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import WatchCredential
from daimon.core.mcp_server_url import ascii_mcp_url
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def save_watch_credential(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_account_id: uuid.UUID,
    server_url: str,
    token_encrypted: bytes,
    now: datetime,
) -> None:
    """Store or replace the owner's key for this server; a replacement is a rotation."""
    await session.execute(
        pg_insert(WatchCredential)
        .values(
            tenant_id=tenant_id,
            owner_account_id=owner_account_id,
            server_url=ascii_mcp_url(server_url),
            token_encrypted=token_encrypted,
            created_at=now,
            rotated_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_watch_credentials_owner_url",
            set_={"token_encrypted": token_encrypted, "rotated_at": now},
        )
    )


async def load_watch_credential(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_account_id: uuid.UUID,
    server_url: str,
    now: datetime | None = None,
) -> bytes | None:
    """The owner's ciphertext for this exact server, or None. `now` records the use."""
    url = ascii_mcp_url(server_url)
    where = (
        WatchCredential.tenant_id == tenant_id,
        WatchCredential.owner_account_id == owner_account_id,
        WatchCredential.server_url == url,
    )
    token = await session.scalar(select(WatchCredential.token_encrypted).where(*where))
    if token is not None and now is not None:
        await session.execute(update(WatchCredential).where(*where).values(last_used_at=now))
    return None if token is None else bytes(token)


async def has_watch_credential(
    session: AsyncSession, *, tenant_id: uuid.UUID, owner_account_id: uuid.UUID, server_url: str
) -> bool:
    return (
        await session.scalar(
            select(WatchCredential.id).where(
                WatchCredential.tenant_id == tenant_id,
                WatchCredential.owner_account_id == owner_account_id,
                WatchCredential.server_url == ascii_mcp_url(server_url),
            )
        )
    ) is not None


async def forget_watch_credential(
    session: AsyncSession, *, tenant_id: uuid.UUID, owner_account_id: uuid.UUID, server_url: str
) -> int:
    """Delete the owner's key for this server; watches using it pause at their next poll."""
    result = await session.execute(
        delete(WatchCredential).where(
            WatchCredential.tenant_id == tenant_id,
            WatchCredential.owner_account_id == owner_account_id,
            WatchCredential.server_url == ascii_mcp_url(server_url),
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def delete_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """Erase every key the account supplied, in the caller's privacy transaction."""
    result = await session.execute(
        delete(WatchCredential).where(WatchCredential.owner_account_id == account_id)
    )
    return int(getattr(result, "rowcount", 0) or 0)
