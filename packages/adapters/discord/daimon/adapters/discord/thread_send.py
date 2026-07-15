"""Archive-safe thread message sending for Discord."""

from __future__ import annotations

import structlog

import discord

log = structlog.get_logger()


async def safe_thread_send(
    thread: discord.Thread,
    content: str,
    *,
    view: discord.ui.View | None = None,
) -> discord.Message:
    """Send to thread; un-archive and retry if thread is archived."""
    try:
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
