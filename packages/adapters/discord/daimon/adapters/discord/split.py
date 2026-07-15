"""Code-fence-aware message splitting for Discord's 2000-char message limit.

Discord's message max is 2000 chars. We split at 1900 to leave headroom. Splits
must not strand an open ``` code fence -- downstream chunks would render as
plain text. When splitting inside a fence, we close it on the chunk we cut and
re-open on the next chunk (preserving the language specifier). When the chunk
is blockquote-styled (`> ` prefix on every line), the prefix is re-applied on
every line including the injected fence markers.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"^```(\S*)\s*$")


def _fence_state(text: str) -> tuple[bool, str]:
    """Return (is_open, language) after scanning *text* linearly.

    *language* is the specifier of the last opened fence (``""`` if none).
    """
    open_ = False
    lang = ""
    for line in text.split("\n"):
        probe = line[2:] if line.startswith("> ") else line
        m = _FENCE_RE.match(probe.rstrip())
        if m:
            if open_:
                open_ = False
            else:
                open_ = True
                lang = m.group(1)
    return open_, lang


def _find_split(window: str) -> int:
    """Prefer paragraph break, then line break, then hard cut at window end."""
    pos = window.rfind("\n\n")
    if pos != -1:
        return pos
    pos = window.rfind("\n")
    if pos != -1:
        return pos
    return len(window)


def split_for_discord_safe(
    text: str,
    limit: int = 1900,
    *,
    blockquote: bool = False,
) -> list[str]:
    """Split *text* into chunks of at most *limit* chars, repairing code fences."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    carry_open = False
    carry_lang = ""

    while len(remaining) > limit:
        window = remaining[:limit]
        cut = _find_split(window)
        if cut == 0:
            cut = limit
        piece = remaining[:cut]
        remaining = remaining[cut:].lstrip("\n")

        prefix_fence = ""
        if carry_open:
            open_marker = f"```{carry_lang}" if carry_lang else "```"
            prefix_fence = f"> {open_marker}\n" if blockquote else f"{open_marker}\n"

        composed = prefix_fence + piece
        is_open, lang = _fence_state(composed)

        suffix_fence = ""
        if is_open:
            close_marker = "```"
            suffix_fence = f"\n> {close_marker}" if blockquote else f"\n{close_marker}"
            carry_open = True
            carry_lang = lang
        else:
            carry_open = False
            carry_lang = ""

        chunks.append(prefix_fence + piece + suffix_fence)

    if remaining:
        prefix_fence = ""
        if carry_open:
            open_marker = f"```{carry_lang}" if carry_lang else "```"
            prefix_fence = f"> {open_marker}\n" if blockquote else f"{open_marker}\n"
        chunks.append(prefix_fence + remaining)

    return chunks
