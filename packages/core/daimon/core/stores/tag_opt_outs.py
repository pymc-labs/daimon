"""People who asked never to be tagged (`daimon.core.outbound.opt_out`), keyed by account."""

from __future__ import annotations

import uuid
from collections.abc import Collection

from daimon.core._models import PlatformPrincipal, TagOptOut
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def set_opt_out(
    session: AsyncSession, *, account_id: uuid.UUID, tenant_id: uuid.UUID
) -> None:
    """Never tag this person; idempotent."""
    await session.execute(
        insert(TagOptOut)
        .values(account_id=account_id, tenant_id=tenant_id)
        .on_conflict_do_nothing()
    )


async def clear_opt_out(session: AsyncSession, *, account_id: uuid.UUID) -> bool:
    """Allow tags again; True when the person had opted out."""
    result = await session.execute(
        delete(TagOptOut).where(TagOptOut.account_id == account_id).returning(TagOptOut.account_id)
    )
    return result.first() is not None


async def is_opted_out(session: AsyncSession, *, account_id: uuid.UUID) -> bool:
    row = await session.execute(
        select(TagOptOut.account_id).where(TagOptOut.account_id == account_id)
    )
    return row.first() is not None


async def opted_out_user_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, user_ids: Collection[str]
) -> frozenset[str]:
    """The platform users among ``user_ids`` whose person opted out.

    A person opted out on one platform is opted out on every identity of the
    same account, so the match goes through the account, not the platform user.
    """
    if not user_ids:
        return frozenset()
    rows = await session.execute(
        select(PlatformPrincipal.external_id)
        .join(TagOptOut, TagOptOut.account_id == PlatformPrincipal.account_id)
        .where(
            PlatformPrincipal.tenant_id == tenant_id,
            PlatformPrincipal.platform == platform,
            PlatformPrincipal.external_id.in_(list(user_ids)),
        )
    )
    return frozenset(rows.scalars().all())


async def count_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    count = await session.scalar(
        select(func.count()).select_from(TagOptOut).where(TagOptOut.account_id == account_id)
    )
    return int(count or 0)


async def delete_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """Erase the person's choice; deleted before the account so the report counts it."""
    result = await session.execute(
        delete(TagOptOut).where(TagOptOut.account_id == account_id).returning(TagOptOut.account_id)
    )
    return len(result.all())
