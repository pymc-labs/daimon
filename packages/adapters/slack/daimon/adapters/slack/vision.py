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

import base64
from typing import NotRequired, TypedDict

import httpx
import structlog
from anthropic.types.beta.sessions import (
    BetaManagedAgentsBase64ImageSourceParam,
    BetaManagedAgentsImageBlockParam,
)

log = structlog.get_logger()

VISION_MEDIA_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)
MAX_VISION_IMAGE_BYTES: int = 5 * 1024 * 1024
MAX_VISION_IMAGE_DIMENSION: int = 8000
MAX_VISION_IMAGES: int = 20


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
    blocks: list[BetaManagedAgentsImageBlockParam] = []
    skipped: list[tuple[SlackFile, str]] = []
    for file in files:
        name = file["name"]
        media_type = file["mimetype"]
        if media_type not in VISION_MEDIA_TYPES:
            log.warning("slack.image.skipped.media_type", name=name, media_type=media_type)
            skipped.append((file, f"unsupported image media type: {media_type or 'unknown'}"))
            continue
        if file["size"] > MAX_VISION_IMAGE_BYTES:
            log.warning("slack.image.skipped.oversize", name=name, size=file["size"])
            skipped.append(
                (file, f"exceeds vision size cap ({file['size']} > {MAX_VISION_IMAGE_BYTES})")
            )
            continue
        if _exceeds_dimension_cap(file):
            log.warning("slack.image.skipped.dimensions", name=name)
            skipped.append((file, f"image dimensions exceed {MAX_VISION_IMAGE_DIMENSION}px"))
            continue
        if len(blocks) >= MAX_VISION_IMAGES:
            log.warning("slack.image.skipped.count_cap", name=name)
            skipped.append((file, f"more than {MAX_VISION_IMAGES} images in one turn"))
            continue
        try:
            resp = await http_client.get(
                file["url_private"], headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as err:
            log.warning("slack.image.skipped.fetch_error", name=name, error=str(err))
            skipped.append((file, f"private-URL fetch failed: {err}"))
            continue
        if not resp.is_success:
            log.warning("slack.image.skipped.fetch_error", name=name, status=resp.status_code)
            skipped.append((file, f"private-URL fetch failed: HTTP {resp.status_code}"))
            continue
        blocks.append(
            BetaManagedAgentsImageBlockParam(
                type="image",
                source=BetaManagedAgentsBase64ImageSourceParam(
                    type="base64",
                    media_type=media_type,  # pyright: ignore[reportArgumentType]  # gated to VISION_MEDIA_TYPES above
                    data=base64.standard_b64encode(resp.content).decode(),
                ),
            )
        )
    return blocks, skipped
