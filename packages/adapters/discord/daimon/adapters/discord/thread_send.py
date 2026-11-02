"""Archive-safe thread message sending for Discord."""

from __future__ import annotations

from typing import Any

import structlog
from daimon.adapters.discord.post_transport import DiscordPostTransport

import discord

log = structlog.get_logger()


async def safe_thread_send(
    thread: discord.Thread,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    transport: DiscordPostTransport | None = None,
) -> discord.Message:
    """Send to thread; un-archive and retry if thread is archived."""
    args = (content,) if content is not None else ()
    kwargs: dict[str, Any] = {}
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    try:
        if transport is not None:
            return await transport.send(*args, **kwargs)
        return await thread.send(*args, **kwargs)
    except discord.HTTPException as exc:
        if exc.code == 50083:  # Thread is archived
            await thread.edit(archived=False)
            log.debug("thread_unarchived", thread_id=thread.id)
            return await thread.send(*args, **kwargs)
        raise
