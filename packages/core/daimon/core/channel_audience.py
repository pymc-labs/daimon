"""Who can read a channel: the one question every hold reader asks.

`channel_has_outsiders` answers whether people outside the team may read a
channel. Today it uses the sticky registry of shared channels only (`shared_channels`):
a channel recorded as shared stays shared, and nothing here writes to the registry.
`audience_mode` reads the server's `audience_hold` setting.

A reader compares the mode by `.value`, never by `.on`: `shadow` counts as on there,
and the mode `shadow` must change nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal, cast

from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.stores.channel_directory import list_shared_channel_ids
from daimon.core.stores.feature_values import MODE_READS_KEY
from daimon.core.stores.shared_channels import is_channel_shared
from sqlalchemy.ext.asyncio import AsyncSession

AudienceMode = Literal["off", "shadow", "on"]
AudienceSource = Literal["registry", "live", "none"]


@dataclass(frozen=True)
class ChannelAudience:
    """What is known about who reads a channel.

    ``contained``: outsiders may read it, or nobody can say. ``known``: the answer
    comes from a recorded fact. ``narrow``: the fail-closed value for a channel the
    answer is unknown for. ``source`` names where the answer came from.
    """

    contained: bool
    known: bool
    narrow: bool
    source: AudienceSource


async def channel_has_outsiders(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, channel_id: str
) -> ChannelAudience:
    """The audience of a channel, from the sticky registry (`channel_id` is the parent channel).

    A recorded channel is contained. A channel with no record is open here: the
    registry holds no fact about it, which is what `is_channel_shared` says today.
    """
    if await is_channel_shared(
        session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
    ):
        return ChannelAudience(contained=True, known=True, narrow=False, source="registry")
    return ChannelAudience(contained=False, known=False, narrow=False, source="none")


async def channels_with_outsiders(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str
) -> set[str]:
    """The channels `channel_has_outsiders` calls contained, in one read, for a channel list."""
    return await list_shared_channel_ids(session, tenant_id=tenant_id, platform=platform)


def mode_of(value: str) -> AudienceMode:
    """The `audience_hold` mode a resolved feature value means; anything else is `off`."""
    if value == "on":
        return "on"
    if value == "shadow":
        return "shadow"
    return "off"


async def audience_mode(
    session: AsyncSession, place: FeaturePlace, *, settings: Settings | None = None
) -> AudienceMode:
    """The server's `audience_hold` mode. A failed read raises: it never falls back to a mode.

    A transaction reads each server once: the policy load, the hold and the write guard share
    the answer. A new transaction, or a feature write in this one, reads it again."""
    # The setting has only deployment and workspace tiers: every room of a server shares it.
    key = (place.tenant_id, place.platform, None if settings is None else id(settings))
    held = cast(
        "tuple[object, dict[object, AudienceMode]] | None", session.info.get(MODE_READS_KEY)
    )
    transaction = session.get_transaction()
    if transaction is not None and held is not None and held[0] is transaction and key in held[1]:
        return held[1][key]
    resolved = await feature_in(session, settings, place, "audience_hold")
    mode = mode_of(resolved.value)
    transaction = session.get_transaction()  # the read itself may have begun it
    if transaction is not None:
        if held is None or held[0] is not transaction:
            held = (transaction, dict[object, AudienceMode]())
            session.info[MODE_READS_KEY] = held
        held[1][key] = mode
    return mode


__all__ = [
    "AudienceMode",
    "ChannelAudience",
    "audience_mode",
    "mode_of",
    "channel_has_outsiders",
    "channels_with_outsiders",
]
