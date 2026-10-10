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


def _find_split(window: str, *, word_boundary: bool = False) -> int:
    """Prefer a paragraph break, then a line break, else cut at the window's end."""
    for separator in ("\n\n", "\n"):
        pos = window.rfind(separator)
        if pos != -1 and (not word_boundary or pos >= len(window) // 2):
            return pos + len(separator) if word_boundary else pos
    if word_boundary:
        spaces = list(re.finditer(r"[ \t]+", window))
        if spaces:
            return spaces[-1].end()
    return len(window)


def split_fenced(
    text: str, limit: int, *, blockquote: bool = False, word_boundary: bool = False
) -> list[str]:
    """Split `text` into chunks of at most `limit` chars, repairing code fences.

    `blockquote` prefixes the injected fence markers with `> ` for an answer
    quoted line by line. ``word_boundary`` prefers whitespace cuts and reserves
    room for repaired fences, so routine messages stay within the platform limit.
    """
    if word_boundary and limit < 16:
        raise ValueError("word-boundary splitting needs a limit of at least 16")
    if len(text) <= limit:
        return [text]
    quote = "> " if blockquote else ""
    chunks: list[str] = []
    remaining, reopen = text, ""
    while len(remaining) + (len(reopen) if word_boundary else 0) > limit:
        # Routine digests keep words together and reserve space for fence repair.
        budget = limit - len(reopen) - len(f"\n{quote}```") if word_boundary else limit
        cut = _find_split(remaining[:budget], word_boundary=word_boundary) or budget
        piece = reopen + remaining[:cut]
        remaining = remaining[cut:] if word_boundary else remaining[cut:].lstrip("\n")
        is_open, lang = _fence_state(piece)
        separator = "" if word_boundary and piece.endswith("\n") else "\n"
        chunks.append(f"{piece}{separator}{quote}```" if is_open else piece)
        reopen = f"{quote}```{lang}\n" if is_open else ""
        if word_boundary and len(reopen) + 4 >= limit:
            # A long language label is optional in a synthetic reopening fence.
            reopen = f"{quote}```\n"
    if remaining:
        chunks.append(reopen + remaining)
    return chunks
