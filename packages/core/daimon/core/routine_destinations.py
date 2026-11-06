"""Whether a person may point a routine at a channel, from the platform's own facts.

A routine posts on its creator's behalf, so a destination must be one the creator
could post in through chat. The chat tools and the web app share these rules and
supply the facts they resolved; delivery applies `routine_delivery`'s same rules
again when the routine fires. Pure.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from daimon.core.channel_visibility import (
    DISCORD_ADMINISTRATOR,
    DISCORD_SEND_MESSAGES,
    DISCORD_VIEW_CHANNEL,
    DiscordMember,
    DiscordOverwrite,
    discord_base_permissions,
    discord_channel_permissions,
)
from daimon.core.routine_delivery import discord_creator_may_post, slack_creator_may_post


def discord_deliverable_channels(
    member: DiscordMember,
    *,
    guild_id: str,
    role_permissions: Mapping[str, int],
    channel_overwrites: Mapping[str, Sequence[DiscordOverwrite]],
    channel_ids: Collection[str],
) -> frozenset[str]:
    """The text channels among `channel_ids` where the member can view and send messages.

    A channel the guild lacks is not deliverable. Threads are not covered: their
    rules need the thread's own facts (see `discord_creator_may_post`).
    """
    base = discord_base_permissions(member, guild_id=guild_id, role_permissions=role_permissions)
    deliverable: set[str] = set()
    for channel_id in channel_ids:
        overwrites = channel_overwrites.get(channel_id)
        if overwrites is None:
            continue
        bits = discord_channel_permissions(
            member, guild_id=guild_id, base_permissions=base, overwrites=overwrites
        )
        if discord_creator_may_post(
            administrator=bool(bits & DISCORD_ADMINISTRATOR),
            view_channel=bool(bits & DISCORD_VIEW_CHANNEL),
            send_messages=bool(bits & DISCORD_SEND_MESSAGES),
            is_thread=False,
            send_messages_in_threads=False,
            is_private_thread=False,
            manage_threads=False,
            is_thread_member=False,
        ):
            deliverable.add(channel_id)
    return frozenset(deliverable)


def slack_may_deliver(
    *,
    bot_is_member: bool,
    is_archived: bool,
    is_im_or_mpim: bool,
    is_private: bool,
    is_guest: bool,
    caller_is_member: bool,
) -> bool:
    """Whether a routine may deliver to a Slack channel for this caller.

    Daimon must be in the channel and the channel not archived, and the caller
    needs the access the channel tools require (`slack_creator_may_post`).
    """
    if is_archived or not bot_is_member:
        return False
    return slack_creator_may_post(
        is_im_or_mpim=is_im_or_mpim,
        is_private=is_private,
        is_guest=is_guest,
        is_member=caller_is_member,
    )
