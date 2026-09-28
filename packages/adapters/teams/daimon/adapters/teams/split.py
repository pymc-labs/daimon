"""Code-fence-aware answer splitting for Teams messages.

A Teams bot message is capped by payload size (about 28 KB), not characters.
4 000 characters stays under it even when JSON escapes each one to six bytes,
as it does non-ASCII text. A split inside a
``` fence closes it on the cut chunk and re-opens it, with its language, on
the next. Duplicated from the Slack adapter: adapters never import each other.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"^```(\S*)\s*$")

TEAMS_LIMIT = 4_000


def _fence_state(text: str) -> tuple[bool, str]:
    """Whether a fence is open at the end of `text`, and the last opened fence's language."""
    is_open, lang = False, ""
    for line in text.split("\n"):
        probe = line[2:] if line.startswith("> ") else line
        if match := _FENCE_RE.match(probe.rstrip()):
            is_open = not is_open
            if is_open:
                lang = match.group(1)
    return is_open, lang


def _find_split(window: str) -> int:
    """Prefer a paragraph break, then a line break, else cut at the window's end."""
    for separator in ("\n\n", "\n"):
        pos = window.rfind(separator)
        if pos != -1:
            return pos
    return len(window)


def split_answer(text: str, limit: int = TEAMS_LIMIT) -> list[str]:
    """Split `text` into chunks of at most `limit` chars, repairing code fences."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining, reopen = text, ""
    while len(remaining) > limit:
        cut = _find_split(remaining[:limit]) or limit
        piece = reopen + remaining[:cut]
        remaining = remaining[cut:].lstrip("\n")
        is_open, lang = _fence_state(piece)
        chunks.append(piece + "\n```" if is_open else piece)
        reopen = f"```{lang}\n" if is_open else ""
    if remaining:
        chunks.append(reopen + remaining)
    return chunks
