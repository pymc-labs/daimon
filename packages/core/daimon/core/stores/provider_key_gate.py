"""Provider key cooldowns and running-turn leases (`daimon.core.provider_key_gate`).

A key is named by a digest, never the key itself. Leases are counted under one
advisory lock per key, so replicas never admit more turns than the caps.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Literal

from daimon.core._models import ProviderKeyCooldown, ProviderKeyLease
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def cooling_until(
    session: AsyncSession, *, provider: str, key_digest: str, model: str, now: datetime
) -> datetime | None:
    """When the key may serve `model` again, or None if it may serve it now."""
    until = await session.scalar(
        select(ProviderKeyCooldown.until).where(
            ProviderKeyCooldown.provider == provider,
            ProviderKeyCooldown.key_digest == key_digest,
            ProviderKeyCooldown.model == model,
        )
    )
    return until if until is not None and until > now else None


async def record_cooldown(
    session: AsyncSession,
    *,
    provider: str,
    key_digest: str,
    model: str,
    now: datetime,
    wait: timedelta,
) -> None:
    """Keep the key cooling until `now + wait`; a longer wait already recorded stays."""
    stmt = insert(ProviderKeyCooldown).values(
        provider=provider, key_digest=key_digest, model=model, until=now + wait
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["provider", "key_digest", "model"],
            set_={"until": func.greatest(ProviderKeyCooldown.until, stmt.excluded.until)},
        )
    )


async def acquire_lease(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider: str,
    key_digest: str,
    now: datetime,
    ttl: timedelta,
    key_cap: int,
    tenant_cap: int,
) -> uuid.UUID | Literal["full"]:
    """A new lease on the key, or "full" when the key or the workspace is at its cap."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"provider-key:{provider}:{key_digest}"},
    )
    await session.execute(
        delete(ProviderKeyLease).where(
            ProviderKeyLease.provider == provider,
            ProviderKeyLease.key_digest == key_digest,
            ProviderKeyLease.expires_at <= now,
        )
    )
    held, held_by_tenant = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(ProviderKeyLease.tenant_id == tenant_id),
            ).where(
                ProviderKeyLease.provider == provider, ProviderKeyLease.key_digest == key_digest
            )
        )
    ).one()
    if held >= key_cap or held_by_tenant >= tenant_cap:
        return "full"
    lease = await session.scalar(
        insert(ProviderKeyLease)
        .values(tenant_id=tenant_id, provider=provider, key_digest=key_digest, expires_at=now + ttl)
        .returning(ProviderKeyLease.id)
    )
    assert lease is not None
    return lease


async def release_lease(session: AsyncSession, lease_id: uuid.UUID) -> None:
    await session.execute(delete(ProviderKeyLease).where(ProviderKeyLease.id == lease_id))
