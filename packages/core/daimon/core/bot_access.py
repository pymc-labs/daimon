"""The `bot_access_wait` feature: work aimed at a channel Daimon cannot reach waits quietly.

With it on, a routine, timer or watch whose channel the bot cannot see or post in is not
failed, paused, counted as an error or sent to anyone by DM. It is held back, and runs
once the bot is given access. Only the bot's own access is read here: a person losing
their permission is a different refusal and keeps its own handling.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Final

import httpx
from daimon.core.channel_post_check import fetch_post_check
from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, place_feature_or_none
from daimon.core.github_credentials import build_multifernet
from daimon.core.support_routing import SupportDestination
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BOT_ACCESS_UNAVAILABLE: Final = "bot_access_unavailable"
"""The reason recorded on a routine's last error or delivery note while it waits."""

BOT_ACCESS_RETRY_DELAY: Final = timedelta(minutes=15)
"""How long a wake waits before it looks again; gentler than the 30 s of a busy thread."""


class BotAccessUnavailable(Exception):
    """The bot cannot see or post in the channel; the work waits and is offered again."""


async def bot_access_wait_on(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, platform: str
) -> bool:
    """Whether the workspace waits for the bot's access; a failed read keeps it off."""
    found = await place_feature_or_none(
        sessionmaker, None, FeaturePlace.of(tenant_id, platform, None), "bot_access_wait"
    )
    return found is not None and found.value == "on"


async def bot_access_blocked(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    workspace_id: str,
    channel_id: str,
) -> bool:
    """True only when the platform says the bot cannot see or post in the channel.

    A thread is checked through its parent channel. A check that cannot be read is not
    a block, so a platform outage never stops work. Teams is never read here.
    """
    if platform not in ("discord", "slack"):
        return False
    crypto_keys = tuple(secret.get_secret_value() for secret in settings.crypto.keys)
    destination = SupportDestination(
        platform=platform,
        channel_id=channel_id,
        own=True,
        slack_team_id=workspace_id if platform == "slack" else None,
    )
    async with httpx.AsyncClient(timeout=10) as http:
        check = await fetch_post_check(
            http,
            destination,
            label=channel_id,
            settings=settings,
            sessionmaker=sessionmaker,
            fernet=build_multifernet(crypto_keys) if crypto_keys else None,
        )
    return check.state == "blocked"
