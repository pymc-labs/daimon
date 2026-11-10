"""The finished-turn cost footer: whether a workspace shows it, and how it is described.

The setting itself is changed in `/settings` (`change_setting`), on every surface.
"""

from __future__ import annotations

import uuid

import structlog
from daimon.core.features.registry import FEATURES
from daimon.core.features.snapshot import FeaturePlace, resolve_one
from daimon.core.stores.feature_values import place_values
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


async def cost_footer_shown(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str | None = None,
    thread_id: str | None = None,
) -> bool:
    """Whether finished turns at this place end with the footer (the `cost_footer`
    feature at its workspace, channel and thread tiers).

    A failed read shows it: the footer is the default, and a turn never loses
    its answer or its close over a display setting.
    """
    place = FeaturePlace.of(tenant_id, platform, channel_id, thread_id)
    try:
        async with session_factory() as session:
            rows = await place_values(
                session,
                tenant_id=tenant_id,
                agent_id=None,
                channel_id=place.channel_id,
                thread_key=place.thread_key,
                keys=("cost_footer",),
            )
        return resolve_one(FEATURES["cost_footer"], None, place, rows).on
    except Exception:
        log.warning("turn.cost_footer_read_failed", exc_info=True)
        return True
