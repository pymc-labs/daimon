"""Which of a channel's messages a fresh mention's turn may be shown.

A top-level mention replays the channel's recent messages, so the turn reads
its own channel the way ``slack_read_channel`` reads one on its behalf: the
channel is asked `authorize(READ_CHANNEL)` for the executing agent from the
turn's origin, and a thread whose readers are limited on its own is withheld,
its root and its broadcast replies alike, by its ``channel_id:thread_ts`` id.

The caller needs no separate visibility check here: Slack delivers a
top-level message only from a member of the channel, and a Slack Connect
sender is refused before any turn starts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, Subject, authorize
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.turn.admission import AdmissionGrant
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ChannelReadPolicy:
    """The tenant policy and the turn facts a channel read is decided on.

    `READ_CHANNEL` decides on the agent and the origin, not on who asks, so no
    subject is kept; the MCP read policy asks with an empty one too.
    """

    policy: TenantAccessPolicy
    agent: AgentRef
    origin: Place
    channel_id: str
    origin_ids: frozenset[str]

    def _allows(self, place: Place) -> bool:
        return bool(
            authorize(
                self.policy,
                subject=Subject(),
                action=Action.READ_CHANNEL,
                agent=self.agent,
                place=place,
                origin_channel_ids=self.origin_ids,
                origin=self.origin,
            )
        )

    def channel_readable(self) -> bool:
        return self._allows(Place(channel_id=self.channel_id))

    def message_readable(self, message: dict[str, Any]) -> bool:
        """A root's thread is its own ts; a broadcast reply carries its thread's."""
        thread_ts = str(message.get("thread_ts") or message.get("ts") or "")
        return self._allows(
            Place(channel_id=f"{self.channel_id}:{thread_ts}", parent_channel_id=self.channel_id)
        )


def origin_ids(channel_id: str, thread_ts: str) -> frozenset[str]:
    """The ids a turn in ``thread_ts`` under ``channel_id`` reads from inside,
    matching the MCP read tools: a Slack thread's own rule is keyed by
    ``channel_id:thread_ts``."""
    return frozenset({channel_id, thread_ts, f"{channel_id}:{thread_ts}"})


async def load_channel_read_policy(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    grant: AdmissionGrant | None,
    channel_id: str,
    thread_ts: str,
) -> ChannelReadPolicy | None:
    """The read decision for ``channel_id`` from this turn, on the policy as it is now.

    None when no history may be shown: the policy can't be read (never treated
    as open), there is no admission grant to decide on, or the turn's agent
    may not read the channel from here.
    """
    if grant is None:
        return None
    try:
        async with sessionmaker() as session:
            policy = await load_access_policy(session, tenant_id=tenant_id)
    # OSError: asyncpg raises a refused or dropped connection unwrapped.
    except (AccessPolicyUnreadable, SQLAlchemyError, OSError) as exc:
        log.warning(
            "slack.channel_context.policy_unreadable",
            tenant_id=str(tenant_id),
            channel_id=channel_id,
            error=str(exc),
        )
        return None
    read_policy = ChannelReadPolicy(
        policy=policy,
        agent=grant.agent,
        origin=grant.run_place,
        channel_id=channel_id,
        origin_ids=origin_ids(channel_id, thread_ts),
    )
    return read_policy if read_policy.channel_readable() else None
