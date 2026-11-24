"""Whether a routine fire into a channel shared with another organisation is held.

A routine's destination is its output's audience. When that channel is recorded
as shared (`shared_channels`), everyone in it, people from another organisation
included, reads what the routine posts. So the fire runs held to that channel
like any turn there: its memory is the channel's own store and its Daimon
credential is the held one (`daimon.core.turn_hold`). It is never run unheld.

A thread saved without its parent channel cannot be placed here; the scheduler
refuses it before any hold is decided (`routine_destination_parent_unknown`).

The hold of a fire is stored with its pending result (`routines.delivery_hold`), so
delivery does not depend on a registry that never forgets: a live fact can clear. With
`audience_hold` on, delivery uses the stored hold or the current fact, whichever is
stricter (`DeliveryGate`). A stored NULL is a legacy result: `off` and `shadow` keep today's
logic, and `on` counts it as unheld.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from daimon.core.audience_gate import audience_fact, log_shadow_refusal, read_audience_mode
from daimon.core.channel_audience import AudienceMode, ChannelAudience, channel_has_outsiders
from daimon.core.fire.subject import RoutineFacts
from daimon.core.rule_views import routine_destination_channel
from daimon.core.stores.domain import RoutineRow
from daimon.core.turn_hold import TurnHold
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def routine_hold(
    session: AsyncSession, row: RoutineFacts, *, platform: str
) -> TurnHold | None:
    """The hold this routine's fire runs under, None when its destination is not shared."""
    channel_id = routine_destination_channel(row)
    if channel_id is None:
        return None
    audience = await channel_has_outsiders(
        session, tenant_id=row.tenant_id, platform=platform, channel_id=channel_id
    )
    if audience.contained:
        return TurnHold(platform, channel_id, client=False)
    return None


async def result_is_held(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: RoutineRow,
    *,
    platform: str,
    parent_channel_id: str | None = None,
) -> bool:
    """Whether a routine's result may only be delivered inside its held channel.

    The same registry the fire read (`routine_hold`), read again at delivery.
    The registry never forgets a shared channel, so a result of a held fire is
    always held here too, and one that became shared since is held as well.
    `parent_channel_id` is the destination's parent when delivery resolved one the
    row did not record (a Discord thread). A held result goes to its destination
    and nowhere else: no direct-message fallback and no notice carrying it.
    """
    async with sessionmaker() as session:
        if await routine_hold(session, row, platform=platform) is not None:
            return True
        if parent_channel_id is None:
            return False
        parent = await channel_has_outsiders(
            session, tenant_id=row.tenant_id, platform=platform, channel_id=parent_channel_id
        )
        return parent.contained


def delivery_hold_value(hold: TurnHold | None) -> dict[str, Any]:
    """What `routines.delivery_hold` stores for a fire run under `hold` (None: not held)."""
    if hold is None:
        return {"held": False}
    return {"held": True, "channel_id": hold.channel_id, "client": hold.client, "relay": hold.relay}


@dataclass(frozen=True)
class StoredHold:
    """A pending result's recorded hold. `recorded` False is a legacy result (stored NULL)."""

    recorded: bool
    hold: TurnHold | None = None


def stored_delivery_hold(row: RoutineRow, *, platform: str) -> StoredHold:
    """The hold the fire of `row`'s pending result ran under.

    A value that cannot be read is an unheld result: a contained destination refuses it.
    """
    raw = row.delivery_hold
    if raw is None:
        return StoredHold(recorded=False)
    channel_id = raw.get("channel_id")
    if raw.get("held") is True and isinstance(channel_id, str) and channel_id:
        return StoredHold(
            recorded=True,
            hold=TurnHold(
                platform,
                channel_id,
                client=raw.get("client") is True,
                relay=raw.get("relay") is True,
            ),
        )
    return StoredHold(recorded=True)


@dataclass(frozen=True)
class DeliveryGate:
    """How `audience_hold` shapes the delivery of one routine result or workflow post.

    `off`: nothing changes. `shadow`: what `on` would refuse is logged. `on`: a result is
    held to its destination when its fire was, and a contained destination takes only a
    held result. A legacy result (nothing recorded) keeps today's logic in `off` and `shadow`;
    `on` counts it as unheld.
    """

    sessionmaker: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    agent: str
    routine_id: uuid.UUID | None
    platform: str
    mode: AudienceMode
    stored: StoredHold

    @property
    def enforcing(self) -> bool:
        return self.mode == "on" and self.stored.recorded

    @property
    def post_hold(self) -> TurnHold | None:
        """The hold `authorize(POST)` takes: the stored one, only when it is enforced."""
        return self.stored.hold if self.enforcing else None

    def keeps_inside(self, held_today: bool) -> bool:
        """The result never leaves its destination, not even by direct message."""
        if self.stored.hold is None:
            return False
        if self.mode == "shadow" and not held_today:
            log_shadow_refusal(
                tenant_id=self.tenant_id,
                channel_id=self.stored.hold.channel_id,
                agent=self.agent,
                reason="held_result_no_direct_message",
                kind="dm_refused",
            )
        return self.enforcing

    async def audience(self, channel_id: str, parent_id: str | None) -> ChannelAudience | None:
        """The destination's fact for `authorize(POST, audience=...)`, None unless enforced.

        In `shadow` a refusal is logged. A failed read raises, so the post fails.
        """
        if not self.stored.recorded or self.mode == "off":
            return None
        return await audience_fact(
            self.sessionmaker,
            tenant_id=self.tenant_id,
            platform=self.platform,
            channel_id=channel_id,
            parent_id=parent_id,
            hold=self.stored.hold,
            agent=self.agent,
            routine_id=self.routine_id,
            mode=self.mode,
        )


async def delivery_gate(
    sessionmaker: async_sessionmaker[AsyncSession], row: RoutineRow, *, platform: str
) -> DeliveryGate:
    """The gate for delivering `row`'s pending result. A failed mode read raises."""
    destination = routine_destination_channel(row)
    mode: AudienceMode = (
        "off"
        if destination is None
        else await read_audience_mode(
            sessionmaker,
            tenant_id=row.tenant_id,
            platform=platform,
            channel_id=destination,
            routine_id=row.id,
        )
    )
    stored = stored_delivery_hold(row, platform=platform)
    if mode == "on" and not stored.recorded:
        # A result queued before the hold was recorded has no provenance: in `on` it counts
        # as unheld, so a contained or unknown destination refuses it.
        stored = StoredHold(recorded=True)
    return DeliveryGate(
        sessionmaker=sessionmaker,
        tenant_id=row.tenant_id,
        agent=row.agent_name,
        routine_id=row.id,
        platform=platform,
        mode=mode,
        stored=stored,
    )


async def workflow_delivery_gate(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    agent: str,
) -> DeliveryGate:
    """The gate for a workflow post into `channel_id`. A failed mode read raises.

    Workflow steps run unheld, so the post is recorded unheld: with `audience_hold` on, a
    channel shared with another organisation refuses it.
    """
    mode = await read_audience_mode(
        sessionmaker, tenant_id=tenant_id, platform=platform, channel_id=channel_id
    )
    return DeliveryGate(
        sessionmaker=sessionmaker,
        tenant_id=tenant_id,
        agent=agent,
        routine_id=None,
        platform=platform,
        mode=mode,
        stored=StoredHold(recorded=True),
    )
