"""Where sign-in sends the browser: only ever to a path on this site."""

from __future__ import annotations

from urllib.parse import quote, urlsplit

DEFAULT_NEXT = "/chat"
_MAX_LENGTH = 512


def safe_next(value: str | None) -> str:
    """`value` if it is a path on this site, otherwise the default landing page.

    Anything that could leave the site is refused: other schemes or hosts,
    protocol-relative `//host`, backslash tricks, control characters and encoded slashes.
    """
    if not value or len(value) > _MAX_LENGTH or not value.startswith("/"):
        return DEFAULT_NEXT
    if value.startswith(("//", "/\\")) or "\\" in value or any(ord(c) < 32 for c in value):
        return DEFAULT_NEXT
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return DEFAULT_NEXT
    return value


def signin_error_url(message: str) -> str:
    return f"/signin?error={quote(message, safe='')}"


def workspaces_url(next_path: str) -> str:
    return f"/workspaces?next={quote(next_path, safe='')}"
