from __future__ import annotations

import uuid

from daimon.adapters.cli.sessions_bootstrap import SessionBootstrapError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def discover_tenant(
    session: AsyncSession,
    *,
    override: uuid.UUID | None = None,
) -> uuid.UUID:
    if override is not None:
        if await get_tenant(session, override) is None:
            raise SessionBootstrapError(
                "db_not_migrated",
                f"tenant {override} not found.",
            )
        return override

    tenant_id = derive_tenant_uuid(platform="cli", workspace_id="local")
    row = await get_tenant(session, tenant_id)
    if row is None:
        raise SessionBootstrapError(
            "defaults_missing",
            "no tenant exists.\n  run: daimon defaults apply",
        )
    return tenant_id
