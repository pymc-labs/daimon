"""The `[attachment]` lines that tell an agent what a person shared with a message.

Pure. Every chat adapter words them here, so the agent reads one phrasing whichever
platform the file came from. The lines are adapter metadata in the user turn and say so
plainly: one posing as a system message reads as prompt injection and gets refused.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class AttachedFile:
    """One file shared with a message, and how the agent can reach it.

    `inlined` files travel as image blocks. Otherwise a `url` is fetched by the agent;
    with none, the file is only named, and `unreadable` says why it can't be opened.
    """

    name: str
    url: str | None = None
    size: int | None = None
    inlined: bool = False
    not_inlined: str | None = None
    unreadable: str | None = None
    expires: str | None = None
    hint: str = ""


def attachment_line(file: AttachedFile) -> str:
    """The one line describing `file`."""
    if file.inlined:
        return f"[attachment] `{file.name}`, shared with this message, is attached as an image."
    if file.url is None:
        here = f": {file.unreadable.rstrip('.')}" if file.unreadable else " here"
        return f"[attachment] `{file.name}` was shared but can't be opened{here}."
    size = "" if file.size is None else f" ({file.size} bytes)"
    skipped = f", not inlined as an image ({file.not_inlined})" if file.not_inlined else ""
    expires = f" (expires in {file.expires})" if file.expires else ""
    hint = f" {file.hint}" if file.hint else ""
    return (
        f"[attachment] `{file.name}`{size}, shared by the user with this message{skipped}. "
        f"Short-lived download URL{expires}, fetch it now (curl to disk, then read it): "
        f"{file.url}{hint}"
    )


def attachment_lines(files: Iterable[AttachedFile]) -> str:
    """One line per file, joined by newlines; empty when nothing was shared."""
    return "\n".join(attachment_line(file) for file in files)
