"""Async store for dm_tenant_selections — the tenant a person's DMs go to.

No try/except — exceptions propagate to the adapter boundary. Callers own the
transaction; every write ends with `await session.flush()`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from daimon.core._models import DmTenantSelection
from daimon.core.stores.domain import DmTenantSelectionRow
from sqlalchemy import CursorResult, delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def get_dm_tenant_selection(
    session: AsyncSession, *, platform: str, external_user_id: str
) -> DmTenantSelectionRow | None:
    """Return the person's stored pick, or None when they never picked."""
    orm = (
        await session.execute(
            select(DmTenantSelection).where(
                DmTenantSelection.platform == platform,
                DmTenantSelection.external_user_id == external_user_id,
            )
        )
    ).scalar_one_or_none()
    return None if orm is None else DmTenantSelectionRow.model_validate(orm)


async def set_dm_tenant_selection(
    session: AsyncSession, *, platform: str, external_user_id: str, tenant_id: uuid.UUID
) -> None:
    """Store or replace the person's pick."""
    statement = insert(DmTenantSelection).values(
        platform=platform, external_user_id=external_user_id, tenant_id=tenant_id
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[DmTenantSelection.platform, DmTenantSelection.external_user_id],
            set_={"tenant_id": statement.excluded.tenant_id, "selected_at": func.now()},
        )
    )
    await session.flush()


async def delete_dm_tenant_selection(
    session: AsyncSession, *, platform: str, external_user_id: str
) -> int:
    """Forget the person's pick, whichever tenant it names. Idempotent."""
    result = await session.execute(
        delete(DmTenantSelection).where(
            DmTenantSelection.platform == platform,
            DmTenantSelection.external_user_id == external_user_id,
        )
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount


async def delete_dm_tenant_selection_for_principal(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, external_user_id: str
) -> int:
    """Purge path: forget the pick only if it names this principal's tenant.

    A pick naming another tenant belongs to the person's identity there, and
    purging one principal must not touch another install's data.
    """
    result = await session.execute(
        delete(DmTenantSelection).where(
            DmTenantSelection.tenant_id == tenant_id,
            DmTenantSelection.platform == platform,
            DmTenantSelection.external_user_id == external_user_id,
        )
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount


async def count_dm_tenant_selections_for_principals(
    session: AsyncSession, *, principal_keys: Sequence[tuple[uuid.UUID, str, str]]
) -> int:
    """Count what the purge path would delete for (tenant_id, platform, user) keys."""
    if not principal_keys:
        return 0
    stmt = (
        select(func.count())
        .select_from(DmTenantSelection)
        .where(
            tuple_(
                DmTenantSelection.tenant_id,
                DmTenantSelection.platform,
                DmTenantSelection.external_user_id,
            ).in_(principal_keys)
        )
    )
    return int((await session.execute(stmt)).scalar_one())
