"""The support channel post: delivery, and the limit lines a settle rewrites.

Shared by Ask a person (`support_escalation`, `support_settle`) and the
routed 👎 form (`feedback_modal`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import structlog
from daimon.adapters.discord.split import split_for_discord_safe
from daimon.core.stores.support_rows import SupportEscalationRow
from daimon.core.support import CLIENT_NOT_NOTIFIED, option_text, post_status_text

import discord
from discord.ext import commands

_log = structlog.get_logger()

_NOT_NOTIFIED_LINE = f"-# {CLIENT_NOT_NOTIFIED}"


CANNOT_SEE: Final = "Daimon can't see this channel. Add it to the channel."
NOT_TEXT: Final = "Pick a text channel."
NEEDS_PERMISSIONS: Final = (
    "Daimon needs View Channel, Send Messages and Embed Links in this channel."
)


async def post_blocker(bot: commands.Bot, channel_id: str) -> str | None:
    """Why Daimon can't post requests in `channel_id`, or None when it can."""
    try:
        channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
    except (discord.HTTPException, ValueError):
        # Not found and forbidden read alike: the bot can't reach it.
        return CANNOT_SEE
    if not isinstance(channel, discord.TextChannel):
        return NOT_TEXT
    can = channel.permissions_for(channel.guild.me)
    return None if can.view_channel and can.send_messages and can.embed_links else NEEDS_PERMISSIONS


@dataclass(frozen=True)
class SupportPost:
    """Where a post landed; `message_id` is its first message, the one carrying any buttons."""

    channel_id: str
    message_id: str | None


async def _send(
    channel: discord.abc.Messageable,
    chunk: str,
    *,
    view: discord.ui.View | None,
    mentions: discord.AllowedMentions | None,
) -> discord.Message:
    """Omits what is unset, so the bot's own defaults apply as before."""
    if view is not None and mentions is not None:
        return await channel.send(chunk, view=view, allowed_mentions=mentions)
    if view is not None:
        return await channel.send(chunk, view=view)
    if mentions is not None:
        return await channel.send(chunk, allowed_mentions=mentions)
    return await channel.send(chunk)


async def post_to_support_channel(
    bot: commands.Bot,
    *,
    channel_id: str,
    body: str,
    allowed_mentions: discord.AllowedMentions | None = None,
    view: discord.ui.View | None = None,
) -> SupportPost | None:
    """Post ``body`` into the support channel; None unless every part landed.

    Resolves through the cache first and falls back to one fetch, because the
    escalation channel lives in the operators' own guild and may not be in a
    freshly-started bot's cache. `view` rides the first message only.
    """
    try:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            channel = await bot.fetch_channel(int(channel_id))
        if not isinstance(channel, discord.abc.Messageable):
            _log.warning("support.channel_not_messageable", channel_id=channel_id)
            return None
        sent = [
            await _send(
                channel, chunk, view=view if index == 0 else None, mentions=allowed_mentions
            )
            for index, chunk in enumerate(split_for_discord_safe(body))
        ]
        first = sent[0] if sent else None
        # A test double may return anything; only a real message has an id worth keeping.
        message_id = str(first.id) if isinstance(first, discord.Message) else None
        return SupportPost(channel_id=channel_id, message_id=message_id)
    except (discord.HTTPException, ValueError) as exc:
        # A misconfigured id, a channel the bot was removed from, or lost
        # send permission. All operator-side, none of them the requester's
        # problem -- and none of them may lose the row.
        _log.warning(
            "support.channel_undeliverable",
            channel_id=channel_id,
            err_type=type(exc).__name__,
            reason=await post_blocker(bot, channel_id),
        )
        return None


def status_line(row: SupportEscalationRow) -> str:
    return post_status_text(
        status=row.status, charged_to=row.charged_to, charge=row.charge, bounds=row.bounds
    )


def request_facts(row: SupportEscalationRow, *, menu: bool, balance: str | None) -> str:
    """A limit request's lines: its terms on a configured menu, status, balance, and while
    pending the decision-8 warning beside the settle buttons."""
    lines = [option_text(row.option)] if menu and row.option is not None else []
    lines.append(status_line(row))
    if balance:
        lines.append(balance)
    if row.status == "pending":
        lines.append(_NOT_NOTIFIED_LINE)
    return "\n".join(lines)


def settled_content(content: str, *, pending_line: str, final_line: str) -> str:
    """The post once settled: the status line replaced, the pending warning gone."""
    lines = [final_line if line == pending_line else line for line in content.split("\n")]
    if final_line not in lines:
        lines.append(final_line)
    return "\n".join(line for line in lines if line != _NOT_NOTIFIED_LINE)


__all__ = [
    "SupportPost",
    "post_to_support_channel",
    "request_facts",
    "settled_content",
    "status_line",
]
