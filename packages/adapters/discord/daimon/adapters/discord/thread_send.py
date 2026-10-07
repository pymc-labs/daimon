"""Archive-safe thread message sending for Discord."""

from __future__ import annotations

import structlog
from daimon.adapters.discord.post_transport import DiscordPostTransport

import discord

log = structlog.get_logger()


async def safe_thread_send(
    thread: discord.Thread,
    content: str,
    *,
    view: discord.ui.View | None = None,
    transport: DiscordPostTransport | None = None,
) -> discord.Message:
    """Send to thread; un-archive and retry if thread is archived."""
    try:
        if transport is not None:
            return await transport.send(content, **({"view": view} if view is not None else {}))
        if view is not None:
            return await thread.send(content, view=view)
        return await thread.send(content)
    except discord.HTTPException as exc:
        if exc.code == 50083:  # Thread is archived
            await thread.edit(archived=False)
            log.debug("thread_unarchived", thread_id=thread.id)
            if view is not None:
                return await thread.send(content, view=view)
            return await thread.send(content)
        raise
