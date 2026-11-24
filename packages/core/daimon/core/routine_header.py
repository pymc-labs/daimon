"""Whether a routine's result post starts with its "Result from ..." header.

The `routine_result_header` feature, resolved for the routine as it runs: its destination
channel, its agent and the routine itself (a workflow's routine step: the workflow). Every
platform's poster asks this one place.
"""

from __future__ import annotations

from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, place_feature
from daimon.core.fire.subject import RoutineFacts, tier_ids
from daimon.core.rule_views import routine_destination_channel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["routine_header_shown"]


async def routine_header_shown(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: RoutineFacts,
    platform: str,
    settings: Settings | None = None,
) -> bool:
    """True unless the feature is off for this routine; an unreadable setting keeps the header.
    Without `settings` the deployment tier comes from the process environment."""
    routine_id, workflow_id = tier_ids(row)
    place = FeaturePlace.of(
        row.tenant_id,
        platform,
        routine_destination_channel(row),
        agent_id=row.agent_id,
        routine_id=routine_id,
        workflow_id=workflow_id,
    )
    found = await place_feature(sessionmaker, settings, place, "routine_result_header")
    return found.value != "off"
