"""Whether workflows are on for a workspace, read the way every entry point reads it."""

from __future__ import annotations

import uuid

from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, resolve_features
from daimon.core.stores import tenants
from daimon.core.workflows.features import KEYS, WorkflowFeatures, workflow_features
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Sessions = async_sessionmaker[AsyncSession]

OFF = "Workflows are off here. Ask an admin to turn them on."


async def features_at(
    sm: Sessions, settings: Settings, tenant_id: uuid.UUID, channel_id: str | None = None
) -> WorkflowFeatures | None:
    """The workflow features of the workspace (or one of its channels); None for no workspace."""
    async with sm() as session:
        tenant = await tenants.get_tenant(session, tenant_id)
    if tenant is None:
        return None
    snapshot = await resolve_features(
        sm, settings, FeaturePlace(tenant.id, tenant.platform, channel_id), keys=KEYS
    )
    return workflow_features(snapshot)


async def enabled(
    sm: Sessions, settings: Settings, tenant_id: uuid.UUID, channel_id: str | None = None
) -> bool:
    features = await features_at(sm, settings, tenant_id, channel_id)
    return features is not None and features.enabled
