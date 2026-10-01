"""Vision routing for Discord image attachments.

Attachments the Anthropic API can consume as vision content blocks are
downloaded from Discord's CDN and forwarded inline on the ``user.message``
event. Everything else — unsupported media types, oversized images, and
non-image files — belongs on the notebook-host upload path, which has its
own size cap and skip reporting. ``is_vision_image_attachment`` is the
single routing predicate for that split.
"""

from __future__ import annotations

import structlog
from anthropic.types.beta.sessions import BetaManagedAgentsImageBlockParam
from daimon.core.media.vision import (
    MAX_VISION_IMAGE_BYTES,
    MAX_VISION_IMAGE_DIMENSION,
    MAX_VISION_IMAGES,
    VISION_MEDIA_TYPES,
    build_image_block,
    sniff_image_media_type,
)

import discord

log = structlog.get_logger()


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


def is_oversized_image(attachment: discord.Attachment) -> bool:
    """True for an image attachment whose pixels exceed the API cap.

    Distinct from ``not is_vision_image_attachment``, which is also True for a
    perfectly small PDF. Callers rendering history need to single out the
    images that will terminate a session if the agent curls and ``read``s them
    at full size.
    """
    return (attachment.content_type or "").startswith("image/") and _exceeds_dimension_cap(
        attachment
    )


def is_vision_image_attachment(attachment: discord.Attachment) -> bool:
    """True when the attachment can be sent as an API vision content block."""
    return (
        (attachment.content_type or "") in VISION_MEDIA_TYPES
        and attachment.size <= MAX_VISION_IMAGE_BYTES
        and not _exceeds_dimension_cap(attachment)
    )


def build_image_url_prefix(attachments: list[discord.Attachment]) -> str:
    """One ``[attachment] ...`` line per image attachment exposing its signed CDN URL.

    Vision blocks give the model pixels but no byte- or URL-level handle: the
    agent can describe an image yet cannot fetch its bytes or pass it to an
    external API. The signed CDN URL (``?ex=&is=&hm=`` params included) is
    publicly fetchable until Discord's signature expires (~24h), so the line
    states that window.
    """
    return "\n".join(
        f"[attachment] image `{attachment.filename}` ({attachment.size} bytes), uploaded by "
        f"the user with this message and forwarded to you inline as a vision block. Signed "
        f"Discord CDN URL (fetchable with curl or passable to external APIs; expires ~24h): "
        f"{attachment.url}"
        for attachment in attachments
    )


def _skipped_for_dimensions(reason: str) -> bool:
    """Was this skip the pixel cap (rather than bytes/count/type/fetch)?

    Matches the reason string `download_as_image_blocks` writes for the
    dimension gate. Keyed on the shared `MAX_VISION_IMAGE_DIMENSION` constant
    rather than a copied literal, so the two cannot drift apart silently.
    """
    return f"{MAX_VISION_IMAGE_DIMENSION}px" in reason


def build_skipped_image_prefix(skipped: list[tuple[discord.Attachment, str]]) -> str:
    """One ``[attachment] ...`` line per image that was NOT inlined as a vision block.

    The base64 block was skipped (too large, too many, unsupported type, fetch
    error), so the model has no pixels for it. The signed CDN URL is still
    fetchable though, and the agent's ``read`` tool renders an image it has
    downloaded to disk — so surfacing the URL keeps the image usable (curl it to
    disk then ``read`` it to view, or hand the URL to an external API) instead of
    silently dropping it.

    **A dimension-skipped image gets a different line, and must.** For every
    other skip reason "curl it and `read` it" is sound advice. For this one it
    is the exact sequence that kills the session: `read` inlines the image at
    full size, the next model request is rejected with "image dimensions exceed
    max allowed size: 8000 pixels", and that rejection is terminal — MA
    terminates the session and every later message in the thread 400s with
    "Cannot send events to archived session".

    That is not hypothetical. Staging session sesn_01TBcsjhyD4KMEc6wasC3vyg
    (2026-08-07) died exactly this way: four images refused inline here, the
    agent curled them to /tmp and `read` all four, and the thread was
    permanently unusable. This gate rejecting the image and then handing over a
    URL with instructions to reintroduce it through an unguarded path made the
    cap self-defeating. So the line below tells the agent to downscale FIRST.
    """
    return "\n".join(
        (
            f"[attachment] image `{attachment.filename}`, uploaded by the user with this "
            f"message, was NOT inlined as a vision block "
            f"({reason}). Do NOT `read` it at full size — that inlines it at its "
            f"original dimensions and the request is rejected terminally, which kills "
            f"this whole conversation. Downscale it first so its longest edge is under "
            f'{MAX_VISION_IMAGE_DIMENSION}px (e.g. python -c "from PIL import Image; '
            f"im=Image.open('in.jpg'); im.thumbnail(({MAX_VISION_IMAGE_DIMENSION - 1000},"
            f"{MAX_VISION_IMAGE_DIMENSION - 1000})); im.save('small.jpg')\"), then `read` "
            f"the downscaled copy. Signed CDN URL (expires ~24h): {attachment.url}"
            if _skipped_for_dimensions(reason)
            else f"[attachment] image `{attachment.filename}`, uploaded by the user with this "
            f"message, was NOT inlined as a vision block "
            f"({reason}); fetch it yourself — signed CDN URL (curl to disk then use your "
            f"`read` tool to view it, or pass to an external API; expires ~24h): "
            f"{attachment.url}"
        )
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
        blocks.append(build_image_block(data, sniffed_media_type))
    return blocks, skipped
