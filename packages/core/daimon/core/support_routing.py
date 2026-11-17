"""Routing for ask-a-human requests: where a request goes, and where it settles.

A workspace's own channel (`support_channels`) wins when the `support_own_channel`
feature allows it; otherwise the deployment's channel for the platform. Both are
read at request time, never cached.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

import structlog
from daimon.core.config import Settings, SupportSettings
from daimon.core.features.deployment import deployment_value
from daimon.core.features.registry import FEATURES
from daimon.core.native_admins import GroupMembers, ServerAdminIds
from daimon.core.stores.support import get_support_channel
from daimon.core.stores.support_rows import SupportEscalationRow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MAX_RECIPIENTS: Final = 10
_log = structlog.get_logger(__name__)


async def support_recipient_tiers(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    requester_id: str,
    members: GroupMembers | None = None,
    server_admins: ServerAdminIds | None = None,
) -> tuple[tuple[str, ...], ...]:
    """No DM tier: the normal escalation channel receives the request."""
    return ()


@dataclass(frozen=True)
class SupportDestination:
    """The channel a request posts to; `own` when the workspace picked it."""

    platform: str
    channel_id: str
    own: bool
    slack_team_id: str | None = None
    teams_team_id: str | None = None
    teams_service_url: str | None = None


def deployment_channel(support: SupportSettings, platform: str) -> str | None:
    """The operator's request channel for one platform."""
    match platform:
        case "discord":
            return support.escalation_channel_id
        case "slack":
            return support.slack_escalation_channel_id
        case "teams":
            return support.teams_escalation_channel_id
        case _:
            return None


def own_channel_allowed(settings: Settings | None) -> bool:
    """The `support_own_channel` feature: may a workspace pick its own request channel."""
    spec = FEATURES.get("support_own_channel")
    if spec is None:
        return False  # until the feature is registered, the deployment keeps the channel
    value = deployment_value(settings, spec)
    return (spec.default if value is None else value.value) == "on"


async def support_destination(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, settings: Settings
) -> SupportDestination | None:
    """Where a request from `platform` in this workspace goes; None when nowhere."""
    if own_channel_allowed(settings):
        own = await get_support_channel(session, tenant_id=tenant_id)
        if own is not None and own.platform == platform:
            return SupportDestination(
                platform=own.platform,
                channel_id=own.channel_id,
                own=True,
                slack_team_id=own.slack_team_id,
                teams_team_id=own.teams_team_id,
                teams_service_url=own.teams_service_url,
            )
    channel = deployment_channel(settings.support, platform)
    if channel is None:
        return None
    team = settings.support.slack_escalation_team_id if platform == "slack" else None
    return SupportDestination(platform=platform, channel_id=channel, own=False, slack_team_id=team)


def in_channel(*, configured: str | None, channel_id: str, parent_id: str | None) -> bool:
    """A click counts from the channel itself or a thread in it."""
    return configured is not None and configured in (channel_id, parent_id)


async def settle_channel_ok(
    session: AsyncSession,
    *,
    row: SupportEscalationRow,
    channel_id: str,
    parent_id: str | None,
    default_channel_id: str | None,
) -> bool:
    """Whether a settle click came from the channel this request was posted to.

    Rows from before the stamp fall back to the workspace's own channel, then
    `default_channel_id` (the deployment's for that platform).
    """
    stamped = row.support_channel_id
    if stamped is None:
        own = await get_support_channel(session, tenant_id=row.tenant_id)
        stamped = own.channel_id if own is not None and own.platform == row.platform else None
    return in_channel(
        configured=stamped or default_channel_id, channel_id=channel_id, parent_id=parent_id
    )


def log_undelivered(row: SupportEscalationRow) -> None:
    """Every platform's error for a recorded request that reached no support channel.

    The row keeps its hold with `delivered_at` NULL; the requests list's undelivered
    filter (CLI, MCP, web) is where an admin finds it.
    """
    _log.error(
        "support.request_undelivered",
        escalation_id=str(row.id),
        tenant_id=str(row.tenant_id),
        platform=row.platform,
        status=row.status,
    )


__all__ = [
    "MAX_RECIPIENTS",
    "SupportDestination",
    "deployment_channel",
    "in_channel",
    "log_undelivered",
    "own_channel_allowed",
    "settle_channel_ok",
    "support_destination",
    "support_recipient_tiers",
]
