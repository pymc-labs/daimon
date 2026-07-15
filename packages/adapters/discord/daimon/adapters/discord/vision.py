"""Vision routing for Discord image attachments.

Attachments the Anthropic API can consume as vision content blocks are
downloaded from Discord's CDN and forwarded inline on the ``user.message``
event. Everything else — unsupported media types, oversized images, and
non-image files — belongs on the notebook-host upload path, which has its
own size cap and skip reporting. ``is_vision_image_attachment`` is the
single routing predicate for that split.
"""

from __future__ import annotations

import base64

import structlog
from anthropic.types.beta.sessions import (
    BetaManagedAgentsBase64ImageSourceParam,
    BetaManagedAgentsImageBlockParam,
)

import discord

log = structlog.get_logger()

# Media types the Anthropic API accepts as image content blocks. Anything
# else (image/svg+xml, image/avif, ...) is rejected by the API and would
# fail the whole user.message event if sent.
VISION_MEDIA_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)

# Per-image API cap (5MB decoded). Larger images are routed to the notebook
# path instead of failing the whole user.message event.
MAX_VISION_IMAGE_BYTES: int = 5 * 1024 * 1024


def sniff_image_media_type(data: bytes) -> str | None:
    """Media type from the image's magic bytes, or None if unrecognized.

    Discord's ``attachment.content_type`` can disagree with the actual bytes
    (e.g. a PNG labeled ``image/webp``). The Anthropic API validates the
    declared media type against the bytes and rejects mismatches — and since
    MA replays every image block on every later turn, one mislabeled image
    permanently terminates the session. The bytes are the truth; sniff them.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# Per-image pixel cap. The Anthropic API rejects the whole request with
# "image dimensions exceed" when any image edge is larger than 8000px. (That
# ceiling drops to 2000px once a request carries more than 20 images — which
# MAX_VISION_IMAGES below keeps us under, so 8000px is the only limit in play.)
# A compressed PNG/WebP can sit well under the byte cap yet still blow past
# this, so dimensions are a separate gate from MAX_VISION_IMAGE_BYTES.
MAX_VISION_IMAGE_DIMENSION: int = 8000

# Per-turn image cap. The API tightens the per-image dimension limit from
# 8000px to 2000px once a request carries more than 20 images. MA persists and
# replays every image block across turns, so the bot only inlines the trigger
# message's images (history images go to the agent as curl-able URLs, not blocks)
# — which keeps a single message's contribution small. This cap is a belt-and-
# suspenders bound on a single trigger message; overflow images are reported as
# skipped (with their URL surfaced) rather than silently dropped.
MAX_VISION_IMAGES: int = 20


def _exceeds_dimension_cap(attachment: discord.Attachment) -> bool:
    """True unless both image edges are known to be within the API's cap.

    An image block that fails model-API validation is replayed by MA on every
    later turn and terminates the session permanently, so "let the API decide"
    is never safe. Unknown dimensions fail closed: the image is not inlined
    and reaches the agent via its signed CDN URL instead.
    """
    width = attachment.width
    height = attachment.height
    if width is None or height is None:
        return True
    return width > MAX_VISION_IMAGE_DIMENSION or height > MAX_VISION_IMAGE_DIMENSION


def is_vision_image_attachment(attachment: discord.Attachment) -> bool:
    """True when the attachment can be sent as an API vision content block."""
    return (
        (attachment.content_type or "") in VISION_MEDIA_TYPES
        and attachment.size <= MAX_VISION_IMAGE_BYTES
        and not _exceeds_dimension_cap(attachment)
    )


def build_image_url_prefix(attachments: list[discord.Attachment]) -> str:
    """One ``*system: ...*`` line per image attachment exposing its signed CDN URL.

    Vision blocks give the model pixels but no byte- or URL-level handle: the
    agent can describe an image yet cannot fetch its bytes or pass it to an
    external API. The signed CDN URL (``?ex=&is=&hm=`` params included) is
    publicly fetchable until Discord's signature expires (~24h), so the line
    states that window.
    """
    return "\n".join(
        f"*system: user attached image `{attachment.filename}` ({attachment.size} bytes), "
        f"forwarded inline as a vision block. Signed CDN URL (fetchable with curl or "
        f"passable to external APIs; expires ~24h): {attachment.url}*"
        for attachment in attachments
    )


def build_skipped_image_prefix(skipped: list[tuple[discord.Attachment, str]]) -> str:
    """One ``*system: ...*`` line per image that was NOT inlined as a vision block.

    The base64 block was skipped (too large, too many, unsupported type, fetch
    error), so the model has no pixels for it. The signed CDN URL is still
    fetchable though, and the agent's ``read`` tool renders an image it has
    downloaded to disk — so surfacing the URL keeps the image usable (curl it to
    disk then ``read`` it to view, or hand the URL to an external API) instead of
    silently dropping it.
    """
    return "\n".join(
        f"*system: image `{attachment.filename}` was NOT inlined as a vision block "
        f"({reason}); fetch it yourself — signed CDN URL (curl to disk then use your "
        f"`read` tool to view it, or pass to an external API; expires ~24h): {attachment.url}*"
        for attachment, reason in skipped
    )


async def download_as_image_blocks(
    attachments: list[discord.Attachment],
) -> tuple[list[BetaManagedAgentsImageBlockParam], list[tuple[discord.Attachment, str]]]:
    """Download image attachments and return base64 vision content blocks.

    Returns ``(blocks, skipped)`` where ``skipped`` is
    ``[(attachment, reason), ...]`` for each attachment that could not be
    turned into a vision block — non-vision input (callers normally
    pre-filter with ``is_vision_image_attachment``), an oversized image, an
    over-the-count-cap image, or a failed CDN fetch. The rest of the batch
    continues; a flaky CDN must not abort the turn. The caller surfaces each
    skipped attachment's URL via ``build_skipped_image_prefix`` so the image
    stays reachable even without a vision block.

    The media-type, byte-size, and dimension gates here defensively re-check
    ``is_vision_image_attachment``'s predicate, and the running count cap
    (``MAX_VISION_IMAGES``) can only be enforced over the whole batch — which is
    this function's job, not the per-attachment predicate's.
    """
    blocks: list[BetaManagedAgentsImageBlockParam] = []
    skipped: list[tuple[discord.Attachment, str]] = []
    for attachment in attachments:
        name = attachment.filename
        media_type = attachment.content_type or ""
        if media_type not in VISION_MEDIA_TYPES:
            log.warning("image_attachment.skipped.media_type", name=name, media_type=media_type)
            skipped.append((attachment, f"unsupported image media type: {media_type or 'unknown'}"))
            continue
        if attachment.size > MAX_VISION_IMAGE_BYTES:
            log.warning("image_attachment.skipped.oversize", name=name, size=attachment.size)
            skipped.append(
                (
                    attachment,
                    f"exceeds vision size cap ({attachment.size} > {MAX_VISION_IMAGE_BYTES})",
                )
            )
            continue
        if _exceeds_dimension_cap(attachment):
            log.warning(
                "image_attachment.skipped.dimensions",
                name=name,
                width=attachment.width,
                height=attachment.height,
            )
            skipped.append(
                (attachment, f"image dimensions unknown or exceed {MAX_VISION_IMAGE_DIMENSION}px")
            )
            continue
        if len(blocks) >= MAX_VISION_IMAGES:
            log.warning("image_attachment.skipped.count_cap", name=name)
            skipped.append((attachment, f"more than {MAX_VISION_IMAGES} images in one turn"))
            continue
        try:
            data = await attachment.read()
        except discord.HTTPException as err:
            log.warning("image_attachment.skipped.fetch_error", name=name, error=str(err))
            skipped.append((attachment, f"CDN fetch failed: {err}"))
            continue
        # Discord's content_type can mislabel the bytes (a PNG declared as
        # image/webp). The API rejects declared-vs-actual mismatches, and MA
        # replays image blocks on every later turn — so a mismatch is a
        # terminal, session-killing error. The sniffed type wins; bytes that
        # match no supported format are skipped rather than sent on trust.
        sniffed_media_type = sniff_image_media_type(data)
        if sniffed_media_type is None:
            log.warning(
                "image_attachment.skipped.unrecognized_bytes",
                name=name,
                declared_media_type=media_type,
            )
            skipped.append(
                (attachment, f"bytes are not a supported image format (declared {media_type})")
            )
            continue
        if sniffed_media_type != media_type:
            log.info(
                "image_attachment.media_type_corrected",
                name=name,
                declared_media_type=media_type,
                sniffed_media_type=sniffed_media_type,
            )
        blocks.append(
            BetaManagedAgentsImageBlockParam(
                type="image",
                source=BetaManagedAgentsBase64ImageSourceParam(
                    type="base64",
                    media_type=sniffed_media_type,
                    data=base64.standard_b64encode(data).decode(),
                ),
            )
        )
    return blocks, skipped
