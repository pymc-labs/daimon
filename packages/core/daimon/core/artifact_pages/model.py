"""Names, ids, limits and the refusal every artifact rule raises."""

from __future__ import annotations

import re
import secrets
from typing import Final, Literal

from daimon.core.errors import UserFacingError

# The tenant-unique name agents use in tools and the readable end of the URL.
SLUG: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
# Lowercase base32: the global, random first segment of `/a/<public_id>/<slug>`.
_PUBLIC_ID_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz234567"
# An escaped NUL in JSON text, not an escaped backslash before "u0000": jsonb refuses it.
_JSON_NUL: Final = re.compile(r"(?<!\\)(?:\\\\)*\\u0000")
MAX_TITLE: Final = 120
_MIB: Final = 1024 * 1024

ErrorCode = Literal[
    "invalid",
    "unauthenticated",
    "not_found",
    "forbidden",
    "too_large",
    "rate_limited",
    "unavailable",
]


class ArtifactError(UserFacingError):
    """A refused request; `code` is what a page's runtime sees as `error.code`."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code


class Limits:
    """Fixed caps. Page data caps are per page, times its `data_scale` (1-10)."""

    HTML_BYTES: Final = 2 * _MIB
    VERSIONS_KEPT: Final = 20
    DOCUMENT_BYTES: Final = 64 * 1024
    DOCUMENTS: Final = 5000
    DATA_BYTES: Final = 20 * _MIB
    MAX_DATA_SCALE: Final = 10
    LIST_DOCUMENTS: Final = 1000
    LIST_BYTES: Final = 4 * _MIB
    EVENTS: Final = 1000
    EVENTS_BYTES: Final = 4 * _MIB
    API_PER_MINUTE: Final = 600
    ASK_PER_MINUTE: Final = 10
    PEOPLE: Final = 200
    EDITORS: Final = 50


def check_slug(slug: str) -> str:
    if SLUG.match(slug) is None:
        raise ArtifactError(
            "invalid", f"Invalid name {slug!r}: 1-63 lowercase letters, digits and inner hyphens."
        )
    return slug


def check_title(title: str) -> str:
    title = title.strip()
    if not 1 <= len(title) <= MAX_TITLE:
        raise ArtifactError("invalid", f"A title is 1-{MAX_TITLE} characters.")
    return title


def has_json_nul(text: str) -> bool:
    return _JSON_NUL.search(text) is not None


def new_public_id() -> str:
    return "".join(secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(10))


def new_link_secret() -> str:
    return secrets.token_urlsafe(24)


def page_url(public_url: str, public_id: str, slug: str, link: str | None = None) -> str:
    """A page's address; `link` is a public page's secret, which the address then carries."""
    url = f"{public_url.rstrip('/')}/a/{public_id}/{slug}"
    return url if link is None else f"{url}?k={link}"
