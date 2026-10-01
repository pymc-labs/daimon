"""Async store for the per-tenant access policy.

Callers own the transaction; writes end with `await session.flush()`.
A missing row is the open default. A row that no longer validates raises
`AccessPolicyUnreadable` so every caller refuses instead of falling open.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from daimon.core._models import Tenant, TenantAccessPolicyRecord
from daimon.core.access_policy import OPEN_ACCESS_POLICY, TenantAccessPolicy
from daimon.core.errors import DaimonError
from pydantic import ValidationError
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


class AccessPolicyUnreadable(DaimonError):
    """The stored policy does not parse; callers must refuse, never assume open."""

    def __init__(self, *, tenant_id: uuid.UUID) -> None:
        # Adapters render this text as-is, so it names no tenant id.
        super().__init__(
            "this workspace's access settings can't be read, so no turn was started; "
            "ask an admin to fix them"
        )
        self.tenant_id = tenant_id


async def lock_access_policy(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """Serialize operator policy edits, including when no policy row exists.

    Call before loading/merging or clearing, in the same transaction as the write.
    """
    (
        await session.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    ).scalar_one()


async def load_access_policy(session: AsyncSession, *, tenant_id: uuid.UUID) -> TenantAccessPolicy:
    # Select the row, not just its payload: a JSON `null` payload would
    # otherwise read as "no row" and fall open.
    record = (
        await session.execute(
            select(TenantAccessPolicyRecord).where(TenantAccessPolicyRecord.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if record is None:
        return OPEN_ACCESS_POLICY
    try:
        # JSON null, an array or a string fail validation like a bad field does.
        return TenantAccessPolicy.model_validate(record.policy)
    except ValidationError as exc:
        raise AccessPolicyUnreadable(tenant_id=tenant_id) from exc


async def set_access_policy(
    session: AsyncSession, *, tenant_id: uuid.UUID, policy: TenantAccessPolicy
) -> None:
    payload = policy.model_dump(mode="json")
    if not payload.get("agent_channel_pins"):
        # Leave the key out when nothing is pinned, so a process built before
        # pins existed still reads the row.
        payload.pop("agent_channel_pins", None)
    await session.execute(
        insert(TenantAccessPolicyRecord)
        .values(tenant_id=tenant_id, policy=payload)
        .on_conflict_do_update(
            index_elements=[TenantAccessPolicyRecord.tenant_id],
            set_={"policy": payload, "updated_at": func.now()},
        )
    )
    await session.flush()


async def clear_access_policy(session: AsyncSession, *, tenant_id: uuid.UUID) -> bool:
    """Delete the tenant's row, putting it back on the open default. True if one existed."""
    result = await session.execute(
        delete(TenantAccessPolicyRecord).where(TenantAccessPolicyRecord.tenant_id == tenant_id)
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount > 0
