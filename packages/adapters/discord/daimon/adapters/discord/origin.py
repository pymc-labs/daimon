"""Where a Discord interaction started, for the inside-only settings rule."""

from __future__ import annotations

from daimon.core.authz import origin_ids

import discord


def interaction_origin(interaction: discord.Interaction) -> frozenset[str]:
    """The channel, and the thread under it, the press came from.

    Discord shows an interaction only to someone who can see its channel
    (`daimon.core.permissions.sealed_outside`).
    """
    channel = interaction.channel
    parent = getattr(channel, "parent_id", None)
    if parent is not None:
        return origin_ids(str(parent), str(interaction.channel_id))
    return origin_ids(str(interaction.channel_id) if interaction.channel_id else None)
