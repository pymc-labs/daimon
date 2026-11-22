"""Whether a routine's result post starts with its "Result from ..." header.

The `routine_result_header` feature, resolved for the routine as it runs: its destination
channel, its agent and the routine itself. Every platform's poster asks this one place.
"""

from __future__ import annotations

from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, place_feature
from daimon.core.rule_views import routine_destination_channel
from daimon.core.stores.domain import RoutineRow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["routine_header_shown"]


async def routine_header_shown(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: RoutineRow,
    platform: str,
    settings: Settings | None = None,
) -> bool:
    """True unless the feature is off for this routine; an unreadable setting keeps the header.
    Without `settings` the deployment tier comes from the process environment."""
    place = FeaturePlace.of(
        row.tenant_id,
        platform,
        routine_destination_channel(row),
        agent_id=row.agent_id,
        routine_id=row.id,
    )
    found = await place_feature(sessionmaker, settings, place, "routine_result_header")
    return found.value != "off"
