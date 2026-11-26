"""Which personal parts (`drive`, `calendar`) a workspace lets Daimon read as a person.

Both are on wherever data sources are on and the deployment has a Google sign-in, until a
workspace admin turns one off with its feature (`PERSONAL_PARTS`). Each person's own switch is
separate (`daimon.data.personal`).
"""

from __future__ import annotations

import uuid

from daimon.core.config import Settings
from daimon.core.data_tools import DATA_SOURCES, PERSONAL_PARTS
from daimon.core.features.snapshot import FeaturePlace, feature_in
from sqlalchemy.ext.asyncio import AsyncSession


async def allowed_parts(
    session: AsyncSession, settings: Settings, tenant_id: uuid.UUID, platform: str
) -> frozenset[str]:
    """The parts the workspace allows, read at its own tier (`platform` picks the platform's
    defaults)."""
    place = FeaturePlace(tenant_id, platform)
    if not (await feature_in(session, settings, place, DATA_SOURCES)).on:
        return frozenset()
    parts: set[str] = set()
    for part, key in PERSONAL_PARTS.items():
        if (await feature_in(session, settings, place, key)).on:
            parts.add(part)
    return frozenset(parts)
