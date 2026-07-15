"""Discord-specific message gating -- pure pre-DB checks."""

from __future__ import annotations


def should_process_message(
    *,
    author_is_bot: bool,
    bot_mentioned: bool,
    guild_id: str | None,
) -> bool:
    """Pre-DB gate checks for on_message. Returns True if message passes all non-DB filters."""
    if author_is_bot:
        return False
    if not bot_mentioned:
        return False
    return guild_id is not None
