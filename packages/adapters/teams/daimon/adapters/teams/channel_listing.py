"""List each installed team's channels into `platform_channel_names`, under their team.

The Web app's Channels page groups a Teams channel by the team recorded on its row, which
otherwise waits for a message in it. A round runs every few hours (`daimon.core.channel_listing`).
Teams has no cheap lookup of one channel by id, so there is no `check_unlisted_channels` step.
"""

from __future__ import annotations

import asyncio

import structlog
from daimon.adapters.teams.billing_panel import TeamChannels
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.core.channel_listing import store_listing
from daimon.core.stores.platform_names import ChannelSighting
from daimon.core.stores.teams_installations import list_teams_installations
from daimon.core.stores.tenants import list_tenants_by_platform
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: Seconds between teams, so a tenant with many teams never bursts the Bot Framework API.
TEAM_GAP_S = 1.0


async def list_all_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    team_channels: TeamChannels,
    *,
    team_gap_s: float = TEAM_GAP_S,
) -> int:
    """One round: every installed team of every Teams tenant, one after another.

    A team whose listing fails is skipped until the next round. Returns how many channels
    were stored.
    """
    stored = 0
    for tenant in await list_tenants_by_platform(sessionmaker, platform="teams"):
        async with sessionmaker() as session:
            teams = await list_teams_installations(session, tenant_id=tenant.id)
        for position, team in enumerate(teams):
            if position:
                await asyncio.sleep(team_gap_s)
            try:
                names = await team_channels(team.team_id)
            except TEAMS_SEND_ERRORS as err:
                log.info("teams.channel_listing.team_failed", error=type(err).__name__)
                continue
            # General's id is its team's, so `team_channels` lists it under the same team.
            stored += await store_listing(
                sessionmaker,
                tenant_id=tenant.id,
                platform="teams",
                channels={
                    channel_id: ChannelSighting(name, parent_id=team.team_id, parent_name=team.name)
                    for channel_id, name in names.items()
                },
            )
    return stored
