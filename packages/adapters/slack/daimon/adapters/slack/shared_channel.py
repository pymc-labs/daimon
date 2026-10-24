"""Slack Connect channels: whether a channel is shared with another organisation.

`conversations.info` `is_ext_shared` is the live signal; `is_shared` alone is
not, because it also covers sharing between workspaces of one organisation. An
envelope's `is_ext_shared_channel` flag says the same without a call. A positive
or negative answer is cached in-process for a few minutes per (team, channel);
a failed lookup is never cached and answers None, so admission falls back to the
shared-channel registry and the requester's own organisation.

`channel_shared` events record the channel in the registry, which is sticky by
design, and drop the cached answer; `channel_unshared` only drops the cached
answer.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Final

import aiohttp
import structlog
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.shared_channels import mark_channel_shared
from daimon.core.stores.tenants import get_tenant
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "ENVELOPE_EXT_SHARED_KEY",
    "SharedChannelProbe",
    "handle_shared_channel_event",
]

log = structlog.get_logger()

#: How long a `conversations.info` answer is trusted.
CACHE_TTL_SECONDS: Final = 600.0

#: Set on an event the envelope marked `is_ext_shared_channel`, so the flag reaches
#: the turn with the event (Slack's own event bodies never carry this key).
ENVELOPE_EXT_SHARED_KEY: Final = "daimon_envelope_ext_shared"

_LOOKUP_ERRORS = (TimeoutError, SlackApiError, aiohttp.ClientError)


class SharedChannelProbe:
    """Answers "is this channel shared with another organisation?", cached per channel."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._answers: dict[tuple[str, str], tuple[float, bool]] = {}

    async def is_shared(
        self,
        client: AsyncWebClient,
        *,
        team_id: str,
        channel_id: str,
        envelope_flag: bool = False,
    ) -> bool | None:
        """True when the platform says the channel is shared with another organisation.

        False is the platform's negative answer; None means it could not be asked.
        """
        if envelope_flag:
            return True
        key = (team_id, channel_id)
        cached = self._answers.get(key)
        if cached is not None and self._clock() < cached[0]:
            return cached[1]
        try:
            response = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        except _LOOKUP_ERRORS as exc:
            log.warning("slack.shared_channel.lookup_failed", channel_id=channel_id, exc_info=exc)
            return None
        channel: Mapping[str, Any] = response.get("channel") or {}  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]  # SlackResponse is untyped
        shared = bool(channel.get("is_ext_shared"))
        self._answers[key] = (self._clock() + CACHE_TTL_SECONDS, shared)
        return shared

    def invalidate(self, *, team_id: str, channel_id: str) -> None:
        """Forget the cached answer, so the next turn asks again."""
        self._answers.pop((team_id, channel_id), None)


async def _record_shared(session: AsyncSession, *, team_id: str, channel_id: str) -> bool:
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    if await get_tenant(session, tenant_id) is None:
        return False
    await mark_channel_shared(
        session, tenant_id=tenant_id, platform="slack", channel_id=channel_id, source="platform"
    )
    return True


async def handle_shared_channel_event(
    sessionmaker: async_sessionmaker[AsyncSession],
    probe: SharedChannelProbe,
    event: Mapping[str, Any],
    *,
    team_id: str,
) -> None:
    """Apply a `channel_shared` or `channel_unshared` event.

    Sharing is recorded and stays recorded, so a channel that stops being shared
    keeps holding turns to itself; only the cached answer is dropped.
    """
    channel_id = str(event.get("channel") or "")
    if not (team_id and channel_id):
        return
    probe.invalidate(team_id=team_id, channel_id=channel_id)
    if event.get("type") != "channel_shared":
        return
    async with sessionmaker.begin() as session:
        recorded = await _record_shared(session, team_id=team_id, channel_id=channel_id)
    log.info("slack.shared_channel.shared_event", channel_id=channel_id, recorded=recorded)
