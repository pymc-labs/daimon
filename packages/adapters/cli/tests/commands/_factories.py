from __future__ import annotations

import uuid

from daimon.core._models import Tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.domain import Platform
from sqlalchemy.ext.asyncio import AsyncSession


async def make_tenant(
    session: AsyncSession,
    *,
    platform: Platform = "discord",
    workspace_id: str | None = None,
) -> Tenant:
    """Create a Tenant row.

    WARNING: Multiple calls without distinct workspace_id will conflict on
    UNIQUE(platform, external_id). Pass explicit workspace_id when a test
    needs more than one tenant.
    """
    wid = workspace_id if workspace_id is not None else str(uuid.uuid4())
    tenant_id = derive_tenant_uuid(platform=platform, workspace_id=wid)
    tenant = Tenant(id=tenant_id, platform=platform, external_id=wid)
    session.add(tenant)
    await session.flush()
    return tenant
