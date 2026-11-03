"""What a channel's page reads beyond its row: spend breakdown, routines, link, subscription.

Spend is billed dollars (model cost times the workspace markup), the unit Credits and
channel budgets use.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import quote, urlencode

from daimon.core.channel_directory import WEB_ROW_ID
from daimon.core.credit_views import billed
from daimon.core.permissions import WEB_PLACE_ID, WEB_PLACE_PREFIX
from daimon.core.stores.domain import RoutineRow, TeamsChannelSubscriptionRow
from daimon.core.stores.routines import list_routines_for_channel
from daimon.core.stores.teams_channel_subscriptions import get_teams_channel_subscription
from daimon.core.stores.usage_events import channel_costs_by_day_agent_user
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class SpendBreakdown:
    """Billed spend since a day, in dollars, three ways. Keys are ids; None is unknown."""

    by_day: dict[date, Decimal]
    by_agent: dict[str | None, Decimal]
    by_person: dict[str | None, Decimal]


def fold_spend(
    costs: dict[tuple[date, str | None, str | None], float], markup: Decimal
) -> SpendBreakdown:
    """Sum (day, agent, person) costs, billed with `markup`, along each axis. Pure."""
    day: defaultdict[date, Decimal] = defaultdict(Decimal)
    agent: defaultdict[str | None, Decimal] = defaultdict(Decimal)
    person: defaultdict[str | None, Decimal] = defaultdict(Decimal)
    for (d, a, p), cost in costs.items():
        value = billed(cost, markup)
        day[d] += value
        agent[a] += value
        person[p] += value
    return SpendBreakdown(dict(sorted(day.items())), dict(agent), dict(person))


async def channel_spend(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    since: datetime,
    markup: Decimal,
) -> SpendBreakdown:
    """The channel's billed spend since `since`; web chat gathers every person's web places."""
    web = channel_id == WEB_ROW_ID
    costs = await channel_costs_by_day_agent_user(
        session,
        tenant_id=tenant_id,
        channel_ids=(WEB_PLACE_ID,) if web else (channel_id,),
        channel_prefix=WEB_PLACE_PREFIX if web else None,
        since=since,
    )
    return fold_spend(costs, markup)


RoutineRelation = Literal["posts_here", "made_here", "both"]


async def channel_routines(
    session: AsyncSession, *, tenant_id: uuid.UUID, channel_id: str
) -> list[tuple[RoutineRow, RoutineRelation]]:
    """Routines that post to the channel or were made in it, each with which."""
    found: list[tuple[RoutineRow, RoutineRelation]] = []
    for row in await list_routines_for_channel(session, tenant_id=tenant_id, channel_id=channel_id):
        posts = row.destination_kind is not None and row.channel_id == channel_id
        made = row.made_in_channel_id == channel_id
        found.append((row, "both" if posts and made else "posts_here" if posts else "made_here"))
    return found


SubscriptionStatus = Literal["active", "expired", "missing"]


async def teams_subscription(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    kind: str | None,
    now: datetime,
) -> tuple[SubscriptionStatus, TeamsChannelSubscriptionRow | None] | None:
    """A Teams private or shared channel's message subscription; None for other kinds."""
    if kind not in ("private", "shared"):
        return None
    row = await get_teams_channel_subscription(session, tenant_id=tenant_id, channel_id=channel_id)
    if row is None:
        return "missing", None
    return ("active" if row.expires_at > now else "expired"), row


def platform_link(
    platform: str,
    *,
    workspace_id: str,
    channel_id: str,
    name: str | None = None,
    group_id: str | None = None,
) -> str | None:
    """The link that opens the channel in its platform's app, or None. Pure."""
    if channel_id == WEB_ROW_ID:
        return None
    if platform == "discord":
        return f"https://discord.com/channels/{workspace_id}/{channel_id}"
    if platform == "slack":
        channel = channel_id.partition(":")[0]
        return "https://slack.com/app_redirect?" + urlencode(
            {"channel": channel, "team": workspace_id}
        )
    if platform == "teams":
        query = {"tenantId": workspace_id} | ({"groupId": group_id} if group_id else {})
        return (
            f"https://teams.microsoft.com/l/channel/{quote(channel_id, safe='')}/"
            f"{quote(name or 'channel', safe='')}?{urlencode(query)}"
        )
    return None


__all__ = [
    "RoutineRelation",
    "SpendBreakdown",
    "SubscriptionStatus",
    "channel_routines",
    "channel_spend",
    "fold_spend",
    "platform_link",
    "teams_subscription",
]
