"""Code-fence-aware splitting of a long answer into platform-sized messages.

A split must not strand an open ``` fence, or later chunks render as plain
text: the cut chunk closes it and the next re-opens it with its language.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"^```(\S*)\s*$")


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


def split_fenced(text: str, limit: int, *, blockquote: bool = False) -> list[str]:
    """Split `text` into chunks of at most `limit` chars, repairing code fences.

    `blockquote` prefixes the injected fence markers with `> ` for an answer
    quoted line by line.
    """
    if len(text) <= limit:
        return [text]
    quote = "> " if blockquote else ""
    chunks: list[str] = []
    remaining, reopen = text, ""
    while len(remaining) > limit:
        cut = _find_split(remaining[:limit]) or limit
        piece = reopen + remaining[:cut]
        remaining = remaining[cut:].lstrip("\n")
        is_open, lang = _fence_state(piece)
        chunks.append(f"{piece}\n{quote}```" if is_open else piece)
        reopen = f"{quote}```{lang}\n" if is_open else ""
    if remaining:
        chunks.append(reopen + remaining)
    return chunks
