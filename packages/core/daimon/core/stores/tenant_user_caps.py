"""Legacy per-(tenant, user) caps. BILL-01.

Caps were folded into spend allowances (`daimon.core.allowance_limits`); the table
stays as a record of what was converted and no code reads it. A row with
`platform_user_id IS NULL` was the tenant-wide default, any other a per-user override.

Per `guideline:architecture`: this module does NOT swallow exceptions.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from daimon.core._models import TenantUserCap
from sqlalchemy import delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def delete_all_for_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
) -> int:
    """GDPR deletion. Deletes ONLY this user's override rows within one tenant.

    The default rows (platform_user_id IS NULL) belong to the tenant, not to
    any user, and are never touched. Postgres `NULL = '<value>'` is NULL (not
    true), so the equality predicate already excludes them.

    `tenant_id` is required: `platform_user_id` is NOT globally unique — Slack
    user ids are workspace-scoped, so `U123` in two workspaces are two different
    humans. Deleting without a tenant filter would erase another tenant's caps.
    """
    result = await session.execute(
        delete(TenantUserCap).where(
            TenantUserCap.tenant_id == tenant_id,
            TenantUserCap.platform_user_id == platform_user_id,
        )
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount
