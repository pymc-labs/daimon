"""Vision routing for Slack image files.

Images the Anthropic API accepts as vision content blocks are downloaded from
Slack's private file URL (auth'd with the workspace bot token) and forwarded
inline on the ``user.message`` event. Everything else — unsupported types,
oversized images, non-image files — is surfaced to the agent as a signed proxy
URL instead (see ``attachments.py``). ``is_vision_image`` is the routing split.

Unlike Discord's public CDN URLs, Slack's ``url_private`` requires the bot
token in an ``Authorization: Bearer`` header, so the byte fetch is authed here.
"""

from __future__ import annotations

import asyncio
from typing import NotRequired, TypedDict

import httpx
import structlog
from anthropic.types.beta.sessions import BetaManagedAgentsImageBlockParam
from daimon.core.media.vision import (
    MAX_VISION_IMAGE_BYTES,
    MAX_VISION_IMAGE_DIMENSION,
    MAX_VISION_IMAGES,
    VISION_MEDIA_TYPES,
    build_image_block,
)

log = structlog.get_logger()

# Private-URL reads in flight per message; a few cut latency without a burst per turn.
_DOWNLOAD_CONCURRENCY = 4


class SlackFile(TypedDict):
    """The subset of a Slack file object the adapter reads.

    ``original_w``/``original_h`` are present only for images; NotRequired.
    """

    id: str
    mimetype: str
    name: str
    size: int
    url_private: str
    url_private_download: str
    original_w: NotRequired[int]
    original_h: NotRequired[int]


def _exceeds_dimension_cap(file: SlackFile) -> bool:
    """True when a known image edge is larger than the API's per-image cap."""
    width = file.get("original_w")
    height = file.get("original_h")
    return (width is not None and width > MAX_VISION_IMAGE_DIMENSION) or (
        height is not None and height > MAX_VISION_IMAGE_DIMENSION
    )


def is_vision_image(file: SlackFile) -> bool:
    """True when the file can be sent as an API vision content block."""
    return (
        file["mimetype"] in VISION_MEDIA_TYPES
        and file["size"] <= MAX_VISION_IMAGE_BYTES
        and not _exceeds_dimension_cap(file)
    )


async def download_as_image_blocks(
    files: list[SlackFile],
    *,
    token: str,
    http_client: httpx.AsyncClient,
) -> tuple[list[BetaManagedAgentsImageBlockParam], list[tuple[SlackFile, str]]]:
    """Download image files and return base64 vision content blocks.

    Returns ``(blocks, skipped)`` where ``skipped`` is ``[(file, reason), ...]``
    for each file that could not be turned into a block — unsupported type,
    oversize, over-dimension, over the per-turn count cap, or a failed fetch.
    The rest of the batch continues; a flaky download must not abort the turn.
    The caller surfaces each skipped file's proxy URL so it stays reachable.
    """
    # Per file, in order: its block, or why it was skipped (None: not yet fetched).
    outcomes: list[BetaManagedAgentsImageBlockParam | str | None] = [
        _static_skip_reason(file) for file in files
    ]
    waiting = [i for i, outcome in enumerate(outcomes) if outcome is None]
    gate = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)

    async def fetch(file: SlackFile) -> BetaManagedAgentsImageBlockParam | str:
        async with gate:
            return await _fetch_image_block(file, token=token, http_client=http_client)

    # Fetch as many as there are free slots, side by side; a failed fetch frees
    # its slot for the next file, as a one-at-a-time loop would.
    inlined = 0
    while waiting and inlined < MAX_VISION_IMAGES:
        free = MAX_VISION_IMAGES - inlined
        window, waiting = waiting[:free], waiting[free:]
        fetched = await asyncio.gather(*(fetch(files[i]) for i in window))
        for i, outcome in zip(window, fetched, strict=True):
            outcomes[i] = outcome
            inlined += not isinstance(outcome, str)
    for i in waiting:
        log.warning("slack.image.skipped.count_cap", name=files[i]["name"])
        outcomes[i] = f"more than {MAX_VISION_IMAGES} images in one message"

    blocks = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    skipped = [
        (file, outcome)
        for file, outcome in zip(files, outcomes, strict=True)
        if isinstance(outcome, str)
    ]
    return blocks, skipped


def _static_skip_reason(file: SlackFile) -> str | None:
    """Why the file's metadata alone rules it out; None when it may be fetched."""
    name = file["name"]
    media_type = file["mimetype"]
    if media_type not in VISION_MEDIA_TYPES:
        log.warning("slack.image.skipped.media_type", name=name, media_type=media_type)
        return f"unsupported image media type: {media_type or 'unknown'}"
    if file["size"] > MAX_VISION_IMAGE_BYTES:
        log.warning("slack.image.skipped.oversize", name=name, size=file["size"])
        return f"exceeds vision size cap ({file['size']} > {MAX_VISION_IMAGE_BYTES})"
    if _exceeds_dimension_cap(file):
        log.warning("slack.image.skipped.dimensions", name=name)
        return f"image dimensions exceed {MAX_VISION_IMAGE_DIMENSION}px"
    return None


async def _fetch_image_block(
    file: SlackFile, *, token: str, http_client: httpx.AsyncClient
) -> BetaManagedAgentsImageBlockParam | str:
    """The file's vision block, or why it could not be built."""
    name = file["name"]
    try:
        resp = await http_client.get(
            file["url_private"], headers={"Authorization": f"Bearer {token}"}
        )
    except httpx.HTTPError as err:
        log.warning("slack.image.skipped.fetch_error", name=name, error=str(err))
        return f"private-URL fetch failed: {err}"
    if not resp.is_success:
        log.warning("slack.image.skipped.fetch_error", name=name, status=resp.status_code)
        return f"private-URL fetch failed: HTTP {resp.status_code}"
    return build_image_block(
        resp.content,
        file["mimetype"],  # pyright: ignore[reportArgumentType]  # gated to VISION_MEDIA_TYPES by _static_skip_reason
    )
