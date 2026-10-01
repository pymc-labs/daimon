"""Inbound Teams attachments: pasted images and files shared in 1:1 chats.

A pasted image is an `image/*` attachment whose `contentUrl` needs the bot's
Bot Framework token; a file shared in a 1:1 chat is `file.download.info` with a
short-lived, pre-authorised SharePoint `downloadUrl`. Images within the vision
limits become image blocks; other files reach the agent as `[attachment]`
lines carrying the download URL. The bot token only goes to Bot Framework
hosts, downloads only come from SharePoint, and no redirect leaves those
hosts. Channel file shares need Microsoft Graph, so the person is told.
"""

from __future__ import annotations

import io
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal, cast

import httpx
import structlog
from anthropic.types.beta.sessions import BetaManagedAgentsImageBlockParam
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.core.errors import DaimonError
from daimon.core.media.filenames import sanitize_title
from daimon.core.media.vision import (
    MAX_VISION_IMAGE_BYTES,
    MAX_VISION_IMAGE_DIMENSION,
    MAX_VISION_IMAGES,
    build_image_block,
    sniff_image_media_type,
)
from microsoft_teams.api import FILE_DOWNLOAD_INFO_CONTENT_TYPE, Attachment
from PIL import Image

log = structlog.get_logger()

MAX_ATTACHMENTS = 10
_MAX_REDIRECTS = 3
_SHAREPOINT_SUFFIXES = (
    ".sharepoint.com",
    ".sharepoint.us",
    ".sharepoint-mil.us",
    ".sharepoint.cn",
)
# Teams serves pasted images from the service URL's host or its media store.
_MEDIA_STORE_SUFFIX = ".asm.skype.com"
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_PIL_FORMATS = ("PNG", "JPEG", "GIF", "WEBP")

BotToken = Callable[[], Awaitable[str | None]]


class FetchRefused(DaimonError):
    """A download the adapter will not make or finish. The message is safe to show."""


@dataclass(frozen=True)
class InboundFile:
    """One attachment. `unreachable` (a channel share) has no fetchable `url`."""

    kind: Literal["pasted_image", "shared_file", "unreachable"]
    name: str
    url: str = ""


@dataclass(frozen=True)
class PreparedAttachments:
    """What a turn gets from its attachments."""

    image_blocks: list[BetaManagedAgentsImageBlockParam]
    prefix: str  # `[attachment]` lines, newline-terminated, for the user message
    notice: str | None  # tells the person what could not be read


def is_sharepoint_host(url: httpx.URL) -> bool:
    return url.scheme == "https" and url.host.endswith(_SHAREPOINT_SUFFIXES)


def _sharepoint_url(value: object) -> str | None:
    """`value` when it is an https SharePoint URL, else None."""
    if not isinstance(value, str):
        return None
    try:
        return value if is_sharepoint_host(httpx.URL(value)) else None
    except httpx.InvalidURL:
        return None


def parse_attachments(
    attachments: Sequence[Attachment], *, personal: bool
) -> tuple[InboundFile, ...]:
    """The images and files on a message. Pure; cards and HTML bodies are ignored."""
    found: list[InboundFile] = []
    for attachment in attachments:
        content_type = attachment.content_type or ""
        if content_type.startswith("image/") and attachment.content_url:
            name = sanitize_title(attachment.name or "image")
            found.append(InboundFile("pasted_image", name, attachment.content_url))
        elif content_type in (FILE_DOWNLOAD_INFO_CONTENT_TYPE, "reference"):
            name = sanitize_title(attachment.name or "file")
            content = attachment.content
            wire = cast("dict[str, object]", content) if isinstance(content, dict) else {}
            url = _sharepoint_url(wire.get("downloadUrl")) if personal else None
            found.append(
                InboundFile("shared_file", name, url) if url else InboundFile("unreachable", name)
            )
    if len(found) > MAX_ATTACHMENTS:
        log.warning("teams.attachments.truncated", count=len(found))
    return tuple(found[:MAX_ATTACHMENTS])


async def fetch_bytes(
    http: httpx.AsyncClient,
    url: str,
    *,
    is_allowed: Callable[[httpx.URL], bool],
    token: str | None,
    max_bytes: int,
) -> bytes:
    """GET `url`, following at most a few redirects, each to an allowed host."""
    target = httpx.URL(url)
    for _ in range(_MAX_REDIRECTS + 1):
        if not is_allowed(target):
            raise FetchRefused(f"{target.host or 'that host'} is not an allowed file host")
        headers = {"Authorization": f"Bearer {token}"} if token else None
        async with http.stream("GET", target, headers=headers, follow_redirects=False) as response:
            if response.next_request is not None:
                target = response.next_request.url
                continue
            if not response.is_success:
                raise FetchRefused(f"download failed with HTTP {response.status_code}")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data += chunk
                if len(data) > max_bytes:
                    raise FetchRefused(f"larger than {max_bytes // (1024 * 1024)} MiB")
            return bytes(data)
    raise FetchRefused("too many redirects")


def image_block(data: bytes) -> BetaManagedAgentsImageBlockParam:
    """An image block, or `FetchRefused` for bytes the vision API would reject."""
    media_type = sniff_image_media_type(data)
    if media_type is None:
        raise FetchRefused("not a PNG, JPEG, GIF or WebP image")
    try:
        with Image.open(io.BytesIO(data), formats=_PIL_FORMATS) as image:
            width, height = image.size
    except (OSError, Image.DecompressionBombError) as err:
        raise FetchRefused("unreadable image") from err
    if max(width, height) > MAX_VISION_IMAGE_DIMENSION:
        raise FetchRefused(f"larger than {MAX_VISION_IMAGE_DIMENSION}px")
    return build_image_block(data, media_type)


def _link_line(file: InboundFile, reason: str | None) -> str:
    skipped = f", not inlined as an image ({reason})" if reason else ""
    warning = ""
    if file.name.lower().endswith(_IMAGE_EXTENSIONS):
        warning = f" Downscale it under {MAX_VISION_IMAGE_DIMENSION}px before viewing it."
    return (
        f"[attachment] `{file.name}`, shared by the user with this message{skipped}. "
        f"Short-lived download URL, fetch it now (curl to disk, then read it): {file.url}{warning}"
    )


async def prepare_attachments(
    http: httpx.AsyncClient,
    files: Sequence[InboundFile],
    *,
    bot_token: BotToken,
    service_url: str | None,
) -> PreparedAttachments:
    """Download what can be inlined; describe the rest. One bad file never aborts the turn."""
    service_host = httpx.URL(service_url).host if service_url else None

    def is_bot_framework(url: httpx.URL) -> bool:
        host = url.host
        return url.scheme == "https" and (
            host == service_host or host.endswith(_MEDIA_STORE_SUFFIX)
        )

    blocks: list[BetaManagedAgentsImageBlockParam] = []
    lines: list[str] = []
    unread: list[str] = []
    for file in files:
        if file.kind == "unreachable":
            lines.append(f"[attachment] `{file.name}` was shared but can't be opened here.")
            unread.append(f"`{file.name}` (files shared in channels need a 1:1 chat)")
            continue
        pasted = file.kind == "pasted_image"
        if not pasted and not file.name.lower().endswith(_IMAGE_EXTENSIONS):
            lines.append(_link_line(file, None))
            continue
        try:
            if len(blocks) >= MAX_VISION_IMAGES:
                raise FetchRefused(f"more than {MAX_VISION_IMAGES} images in one message")
            data = await fetch_bytes(
                http,
                file.url,
                is_allowed=is_bot_framework if pasted else is_sharepoint_host,
                token=await bot_token() if pasted else None,
                max_bytes=MAX_VISION_IMAGE_BYTES,
            )
            blocks.append(image_block(data))
        except (FetchRefused, httpx.InvalidURL, *TEAMS_SEND_ERRORS) as err:
            # Never log or show the URL: a download URL is itself a credential.
            reason = str(err) if isinstance(err, FetchRefused) else type(err).__name__
            log.warning("teams.attachment.skipped", kind=file.kind, reason=reason)
            if not pasted:
                lines.append(_link_line(file, reason))
                continue
            lines.append(f"[attachment] pasted image `{file.name}` was not inlined ({reason}).")
            unread.append(f"`{file.name}` ({reason})")
    return PreparedAttachments(
        image_blocks=blocks,
        prefix="".join(f"{line}\n" for line in lines),
        notice=f"I couldn't read {', '.join(unread)}." if unread else None,
    )
