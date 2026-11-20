"""The hidden eval tenant: one per deployment, where eval runs keep their sessions and state
apart from every real workspace. Created on first use."""

from __future__ import annotations

import uuid
from typing import Final

from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

EVAL_WORKSPACE: Final = "daimon-evals"
EVAL_TENANT_ID: Final[uuid.UUID] = derive_tenant_uuid(platform="cli", workspace_id=EVAL_WORKSPACE)


def is_eval_tenant(tenant_id: uuid.UUID) -> bool:
    return tenant_id == EVAL_TENANT_ID


async def eval_tenant(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """The eval tenant's id, creating it if this deployment has none yet."""
    result = await provision_tenant(sessions, platform="cli", workspace_id=EVAL_WORKSPACE)
    return result.tenant_id
