"""Turns held to one channel: people from another organisation, and shared channels.

A turn is *held* to its origin channel when the person asking is from another
organisation than the workspace that added Daimon (a *client* requester), or
when the channel itself is shared with another organisation. Everything a held
turn reads or posts stays inside that channel and the threads under it, and it
carries no administrator authority. Pure: no I/O.

The facts come from the platform, never from the model: the adapter says which
organisation the requester belongs to (`RequesterOrg`), admission decides the
hold, and the hold travels with the turn's own MCP credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class RequesterOrg(StrEnum):
    """Which organisation the platform says the requester belongs to."""

    #: A verified member of the workspace, Teams tenant or Discord server that added Daimon.
    HOST = "host"
    #: Verified to belong to another organisation.
    OTHER = "other"
    #: The platform could not say. Treated as another organisation for this turn only.
    UNKNOWN = "unknown"


#: How each platform names a thread under a channel, after the channel id.
#: Slack threads are ``<channel>:<ts>``; Teams threads ``<conversation>;messageid=<root>``.
#: Discord threads have ids of their own and are matched by their parent.
_THREAD_SEPARATORS: Final[dict[str, str]] = {"slack": ":", "teams": ";messageid="}


@dataclass(frozen=True)
class TurnHold:
    """The channel a held turn stays in.

    ``channel_id`` is the parent channel; threads under it are inside the hold.
    ``client`` says the requester is from another organisation (or unknown),
    which also refuses everything a plain member could do outside channels.
    """

    platform: str
    channel_id: str
    client: bool

    def contains(self, channel_id: str | None, parent_channel_id: str | None = None) -> bool:
        """Whether a target channel or thread lies inside the held channel."""
        if channel_id is None:
            return False
        if self.channel_id in (channel_id, parent_channel_id):
            return True
        separator = _THREAD_SEPARATORS.get(self.platform)
        return separator is not None and channel_id.startswith(self.channel_id + separator)


__all__ = ["RequesterOrg", "TurnHold"]
