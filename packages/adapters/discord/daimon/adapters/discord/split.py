"""Answer splitting for Discord's 2000-char message limit (1900 leaves headroom)."""

from __future__ import annotations

from daimon.core.message_split import split_fenced


def split_for_discord_safe(text: str, limit: int = 1900, *, blockquote: bool = False) -> list[str]:
    """Split *text* into chunks of at most *limit* chars, repairing code fences."""
    return split_fenced(text, limit, blockquote=blockquote)


def split_discord_answer(text: str, limit: int = 1900) -> list[str]:
    """Number every part of a long answer, outside its repaired code fences."""
    chunks = split_for_discord_safe(text, limit=limit)
    if len(chunks) == 1:
        return chunks
    # Reserve space before splitting, including for very large part counts.
    chunks = split_for_discord_safe(text, limit=limit - 32)
    return [f"({index}/{len(chunks)})\n{chunk}" for index, chunk in enumerate(chunks, start=1)]
