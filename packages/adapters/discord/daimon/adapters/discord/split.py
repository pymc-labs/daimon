"""Answer splitting for Discord's 2000-char message limit (1900 leaves headroom)."""

from __future__ import annotations

from daimon.core.message_split import split_fenced


def split_for_discord_safe(text: str, limit: int = 1900, *, blockquote: bool = False) -> list[str]:
    """Split *text* into chunks of at most *limit* chars, repairing code fences."""
    return split_fenced(text, limit, blockquote=blockquote)
