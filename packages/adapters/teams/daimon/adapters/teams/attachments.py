"""Inbound Teams attachments: pasted images and files shared in 1:1 chats.

A pasted image is an `image/*` attachment whose `contentUrl` needs the bot's
Bot Framework token; a file shared in a 1:1 chat is `file.download.info` with a
short-lived, pre-authorised SharePoint `downloadUrl`. Images within the vision
limits become image blocks; other files reach the agent as `[attachment]`
lines carrying the download URL. The bot token only goes to Bot Framework
hosts, downloads only come from SharePoint, and no redirect leaves those
hosts.

A channel activity carries only the message's text, so every channel message
is read from Microsoft Graph: images from its hosted content, with the Graph
token and only from the Graph host. `<img>` and `<attachment>` tags in the
activity's `text/html` body are counted only to tell the person what was
missed when Graph cannot be read. Channel files live in SharePoint, read
only from the team's own site once an admin grants it (`channel_files`).
What cannot be read reaches the agent as an `[attachment]` line saying why,
for its answer to explain: nothing is posted on its own.
"""

from __future__ import annotations

import io
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
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
from daimon.core.teams_graph import is_graph_url, is_sharepoint_host
from microsoft_teams.api import FILE_DOWNLOAD_INFO_CONTENT_TYPE, Attachment
from PIL import Image

log = structlog.get_logger()

MAX_ATTACHMENTS = 10
_MAX_REDIRECTS = 3
# Teams serves pasted images from the service URL's host or its media store.
_MEDIA_STORE_SUFFIX = ".asm.skype.com"
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_PIL_FORMATS = ("PNG", "JPEG", "GIF", "WEBP")

BotToken = Callable[[], Awaitable[str | None]]


class FetchRefused(DaimonError):
    """A download the adapter will not make or finish. The message is safe to show."""


@dataclass(frozen=True)
class InboundFile:
    """One attachment. `unreachable` and `refused` (a channel share daimon has no
    access to) have no fetchable `url`; an `embedded_*` one is only known from a
    channel message's HTML, until Graph names it."""

    kind: Literal[
        "pasted_image",
        "shared_file",
        "unreachable",
        "refused",
        "embedded_image",
        "embedded_file",
        "graph_image",
    ]
    name: str
    url: str = ""


@dataclass(frozen=True)
class SharedFile:
    """A file shared in a channel: its SharePoint URL, and a download URL once resolved.

    `refused` when Graph denied daimon the team's files."""

    name: str
    content_url: str | None = None
    download_url: str | None = None
    refused: bool = False


@dataclass(frozen=True)
class ChannelMedia:
    """What a channel message carries per Graph: hosted image URLs and shared files,
    and the team's group id they were read under."""

    image_urls: tuple[str, ...] = ()
    files: tuple[SharedFile, ...] = ()
    group_id: str | None = None


class _EmbeddedCounter(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images = self.files = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "attachment":
            self.files += 1
        elif tag == "img" and "emoji" not in (dict(attrs).get("itemtype") or "").lower():
            self.images += 1


def _embedded(attachments: Sequence[Attachment]) -> _EmbeddedCounter:
    counter = _EmbeddedCounter()
    for attachment in attachments:
        if attachment.content_type == "text/html" and isinstance(attachment.content, str):
            counter.feed(attachment.content)
    counter.close()
    return counter


@dataclass(frozen=True)
class PreparedAttachments:
    """What a turn gets from its attachments."""

    image_blocks: list[BetaManagedAgentsImageBlockParam]
    prefix: str  # `[attachment]` lines, newline-terminated, for the user message


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
    """The images and files on a message. Pure; cards are ignored."""
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
    if not personal:
        # What the HTML body holds beyond the attachments Teams also listed.
        embedded = _embedded(attachments)
        listed_images = sum(file.kind == "pasted_image" for file in found)
        listed_files = sum(file.kind == "unreachable" for file in found)
        found += [InboundFile("embedded_image", "image")] * max(0, embedded.images - listed_images)
        found += [InboundFile("embedded_file", "file")] * max(0, embedded.files - listed_files)
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


def _resolve_embedded(
    files: Sequence[InboundFile], media: ChannelMedia | None
) -> tuple[list[InboundFile], list[InboundFile]]:
    """`(files to read, embedded ones Graph could not name)`. Graph's view of the
    messages, when there is one, replaces what the activities listed for a channel."""
    if media is None:
        embedded = [f for f in files if f.kind in ("embedded_image", "embedded_file")]
        return [f for f in files if f not in embedded], embedded
    kept = [InboundFile("graph_image", "image", url) for url in media.image_urls]
    kept += [
        InboundFile("shared_file", sanitize_title(f.name), f.download_url)
        if f.download_url
        else InboundFile("refused" if f.refused else "unreachable", sanitize_title(f.name))
        for f in media.files
    ]
    return kept, []


async def _inline_history(
    http: httpx.AsyncClient,
    urls: Sequence[str],
    blocks: list[BetaManagedAgentsImageBlockParam],
    graph_token: BotToken | None,
) -> str:
    """Append the history's images to `blocks`; the line that tells the agent so."""
    inlined = 0
    for url in urls:
        try:
            if len(blocks) >= MAX_VISION_IMAGES or graph_token is None:
                raise FetchRefused("no room" if graph_token else "no Graph token")
            data = await fetch_bytes(
                http,
                url,
                is_allowed=is_graph_url,
                token=await graph_token(),
                max_bytes=MAX_VISION_IMAGE_BYTES,
            )
            blocks.append(image_block(data))
            inlined += 1
        except (FetchRefused, httpx.InvalidURL, *TEAMS_SEND_ERRORS) as err:
            reason = str(err) if isinstance(err, FetchRefused) else type(err).__name__
            log.warning("teams.attachment.skipped", kind="history_image", reason=reason)
    missed = len(urls) - inlined
    return (
        f"[attachment] {inlined} image(s) from earlier messages are attached after the "
        "person's own, in history order; `images_attached` on a replayed message counts its own."
        + (f" {missed} could not be fetched, so the counts overstate." if missed else "")
    )


async def prepare_attachments(
    http: httpx.AsyncClient,
    files: Sequence[InboundFile],
    *,
    bot_token: BotToken,
    service_url: str | None,
    channel: bool = False,
    channel_media: ChannelMedia | None = None,
    graph_token: BotToken | None = None,
    history_images: Sequence[str] = (),
) -> PreparedAttachments:
    """Download what can be inlined; describe the rest. One bad file never aborts the turn.

    `channel_media` is the channel message as Graph sees it, or None when it
    could not be read; `graph_token` authorises its hosted images and the
    replayed history's `history_images`, inlined after the message's own.
    """
    service_host = httpx.URL(service_url).host if service_url else None
    files, unnamed = _resolve_embedded(files, channel_media)

    def is_bot_framework(url: httpx.URL) -> bool:
        host = url.host
        return url.scheme == "https" and (
            host == service_host or host.endswith(_MEDIA_STORE_SUFFIX)
        )

    blocks: list[BetaManagedAgentsImageBlockParam] = []
    lines: list[str] = []
    if channel and channel_media is None and not unnamed:
        # The activity names no media, so whether any were missed is unknown.
        lines.append(
            "[attachment] This channel message's images and files could not be read; "
            "say so if the person refers to one."
        )
    for file in unnamed:
        what = "an image" if file.kind == "embedded_image" else "a file"
        lines.append(
            f"[attachment] {what} was shared but can't be opened: "
            "daimon could not read this channel message."
        )
    for file in files:
        if file.kind == "refused":
            lines.append(
                f"[attachment] `{file.name}` was shared but can't be opened: daimon has no "
                "access to this team's files until a Microsoft 365 admin grants it."
            )
            continue
        if file.kind == "unreachable":
            lines.append(
                f"[attachment] `{file.name}` was shared but can't be opened: "
                "daimon could not fetch it from this channel."
            )
            continue
        pasted = file.kind in ("pasted_image", "graph_image")
        if not pasted and not file.name.lower().endswith(_IMAGE_EXTENSIONS):
            lines.append(_link_line(file, None))
            continue
        try:
            if len(blocks) >= MAX_VISION_IMAGES:
                raise FetchRefused(f"more than {MAX_VISION_IMAGES} images in one message")
            # Each token only ever goes to its own hosts; SharePoint URLs carry their own.
            if file.kind == "graph_image":
                is_allowed, token = is_graph_url, await graph_token() if graph_token else None
                if token is None:
                    raise FetchRefused("no Graph token")
            elif pasted:
                is_allowed, token = is_bot_framework, await bot_token()
            else:
                is_allowed, token = is_sharepoint_host, None
            data = await fetch_bytes(
                http, file.url, is_allowed=is_allowed, token=token, max_bytes=MAX_VISION_IMAGE_BYTES
            )
            blocks.append(image_block(data))
            # Unnamed in the text, an image block reads as part of the prompt, not as shared.
            lines.append(
                f"[attachment] `{file.name}`, shared by the user with this message, "
                "is attached as an image."
            )
        except (FetchRefused, httpx.InvalidURL, *TEAMS_SEND_ERRORS) as err:
            # Never log or show the URL: a download URL is itself a credential.
            reason = str(err) if isinstance(err, FetchRefused) else type(err).__name__
            log.warning("teams.attachment.skipped", kind=file.kind, reason=reason)
            if not pasted:
                lines.append(_link_line(file, reason))
                continue
            lines.append(f"[attachment] pasted image `{file.name}` was not inlined ({reason}).")
    if history_images:
        lines.append(await _inline_history(http, history_images, blocks, graph_token))
    return PreparedAttachments(image_blocks=blocks, prefix="".join(f"{line}\n" for line in lines))
