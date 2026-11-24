"""The page bundle (version 1) another system exports, and what a deployment cannot run.

One JSON file per page. `Bundle` is the contract between an exporter and
`daimon artifacts import`: unknown fields are refused, so a format change never
slips through. `compat_warnings` reads a page's HTML for what this deployment's
page policy would break; it reports host names and page names, never keys.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from typing import Annotated, Final, Literal
from urllib.parse import urlsplit

from daimon.core.artifact_pages.content import ASSET_HOSTS, PLAYER_FRAMES
from daimon.core.artifact_pages.model import MAX_TITLE, Limits
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    StringConstraints,
    model_validator,
)

_ID = Annotated[str, StringConstraints(pattern=r"[0-9]{1,20}")]
_PAGE_FIELD = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class DiscordRef(BaseModel):
    model_config = _PAGE_FIELD

    discord_id: _ID


class PersonRef(BaseModel):
    """A chat member (`discord_id`), or someone known by email who becomes a visitor.
    `google_sub` pins that visitor's Google sign-in."""

    model_config = _PAGE_FIELD

    discord_id: _ID | None = None
    email: Annotated[str, StringConstraints(max_length=254, pattern=r"[^@\s]+@[^@\s]+")] | None = (
        None
    )
    google_sub: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> PersonRef:
        if (self.discord_id is None) == (self.email is None):
            raise ValueError("a person has a discord_id or an email, not both or neither")
        if self.google_sub is not None and self.email is None:
            raise ValueError("google_sub goes with an email")
        return self


class VersionIn(BaseModel):
    model_config = _PAGE_FIELD

    version: int = Field(ge=1)
    html: str
    published_by: DiscordRef | None = None
    created_at: AwareDatetime


class DocumentIn(BaseModel):
    model_config = _PAGE_FIELD

    collection: str
    doc_id: str
    data: dict[str, JsonValue]
    updated_by: str | None = None
    updated_by_ref: DiscordRef | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class WatchIn(BaseModel):
    """A page-event watch. `select` is a Daimon projection path such as `.data.item`."""

    model_config = _PAGE_FIELD

    legacy_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    name: str = ""
    instructions: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    owner: DiscordRef
    channel_id: _ID
    thread_id: _ID | None = None
    place_type: Literal["channel", "thread", "direct-message", "mcp", "voice", "routine"]
    delivery: Literal["thread", "new_thread"]
    select: str | None = None
    max_turns_per_day: int | None = Field(default=None, ge=1, le=240)
    expires_at: AwareDatetime | None = None
    paused: bool = False


class PageIn(BaseModel):
    model_config = _PAGE_FIELD

    legacy_url: Annotated[str, StringConstraints(pattern=r"https?://[^\s]+", max_length=2000)]
    public_id: Annotated[str, StringConstraints(pattern=r"[a-z2-7]{10}")]
    slug: Annotated[str, StringConstraints(pattern=r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")]
    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TITLE)
    ]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    owner: DiscordRef
    editors: list[DiscordRef] = Field(default_factory=list[DiscordRef])
    editors_can_share: bool = True
    access: Literal["workspace", "people", "public"]
    people: list[PersonRef] = Field(default_factory=list[PersonRef])
    link_secret: SecretStr | None = Field(default=None, repr=False)
    expires_at: AwareDatetime | None = None
    data: Literal["off", "read", "write"] = "off"
    events: bool = False
    data_scale: int = Field(default=1, ge=1, le=Limits.MAX_DATA_SCALE)
    data_revision: int = Field(default=0, ge=0)
    current_version: int = Field(ge=1)
    versions: list[VersionIn] = Field(min_length=1)
    documents: list[DocumentIn] = Field(default_factory=list[DocumentIn])
    watches: list[WatchIn] = Field(default_factory=list[WatchIn])

    @model_validator(mode="after")
    def _consistent(self) -> PageIn:
        numbers = [v.version for v in self.versions]
        if len(set(numbers)) != len(numbers) or self.current_version not in numbers:
            raise ValueError("versions must be distinct and include current_version")
        if self.access == "public" and (
            self.data == "write" or (self.data == "read" and self.events)
        ):
            raise ValueError("a public page cannot write data or both read data and send events")
        return self


class Bundle(BaseModel):
    """One page's file: `{"version": 1, "guild_id", "exported_at", "page": {...}}`."""

    model_config = _PAGE_FIELD

    version: Literal[1]
    guild_id: _ID
    exported_at: AwareDatetime
    page: PageIn


_URL_ATTR: Final = re.compile(
    r"""\b(?:src|href|poster|data)\s*=\s*["']?\s*((?:https?:)?//[^\s"'>]+)"""
)
_TAG: Final = re.compile(
    r"<(script|img|link|source|video|audio|iframe|embed|object)\b[^>]*>", re.IGNORECASE
)
_CSS_URL: Final = re.compile(
    r"""url\(\s*["']?((?:https?:)?//[^\s"')]+)|@import\s+["']((?:https?:)?//[^"']+)"""
)
_STORAGE: Final = re.compile(r"\b(?:localStorage|sessionStorage|indexedDB)\b|document\.cookie")


def _allowed(url: str, sources: Sequence[str]) -> bool:
    """CSP host-source matching: a source with a path is a prefix, one without is a host,
    and `https://*.example.com` is any subdomain."""
    parts = urlsplit(url)
    for source in sources:
        wanted = urlsplit(source)
        if wanted.path not in ("", "/"):
            if url.startswith(source):
                return True
        elif parts.scheme == wanted.scheme and (
            parts.netloc == wanted.netloc
            or (
                (wanted.hostname or "").startswith("*.")
                and (parts.hostname or "").endswith((wanted.hostname or "")[1:])
            )
        ):
            return True
    return False


def _hosts(urls: Collection[str]) -> str:
    return ", ".join(sorted({urlsplit(u).netloc for u in urls}))


def compat_warnings(
    html: str,
    *,
    asset_hosts: Sequence[str],
    content_host: bool,
    legacy_url: str,
    known_slugs: Collection[str],
) -> list[str]:
    """What in this page the deployment's policy would break. `asset_hosts` are the
    operator's extra sources, `content_host` whether pages get an origin of their own, and
    `known_slugs` the pages being imported with it."""
    sources = (*ASSET_HOSTS, *asset_hosts)
    blocked: set[str] = set()
    frames: set[str] = set()
    for tag in _TAG.finditer(html):
        for found in _URL_ATTR.finditer(tag[0]):
            url = found[1] if found[1].startswith("http") else f"https:{found[1]}"
            if tag[1].lower() in ("iframe", "embed", "object"):
                if not _allowed(url, PLAYER_FRAMES) or not content_host:
                    frames.add(url)
            elif not _allowed(url, sources):
                blocked.add(url)
    for css in _CSS_URL.finditer(html):
        url = css[1] or css[2]
        url = url if url.startswith("http") else f"https:{url}"
        if not _allowed(url, sources):
            blocked.add(url)
    warnings: list[str] = []
    if blocked:
        warnings.append(f"loads from hosts this deployment blocks: {_hosts(blocked)}")
    if frames:
        warnings.append(
            f"embeds frames that need a content domain or are never allowed: {_hosts(frames)}"
        )
    if not content_host and _STORAGE.search(html):
        warnings.append("uses browser storage, which fails without a content domain")
    host = urlsplit(legacy_url).netloc
    links = {
        m[1]
        for m in re.finditer(rf"https?://{re.escape(host)}/a/([a-z0-9][a-z0-9-]*)", html)
        if m[1] not in known_slugs
    }
    if links:
        warnings.append(f"links to pages outside this import: {', '.join(sorted(links))}")
    return warnings
