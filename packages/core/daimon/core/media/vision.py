"""Model-API vision limits and image content blocks, shared by every chat adapter.

Pure: no I/O. An image block that fails model-API validation is replayed by
Managed Agents on every later turn and terminates the session, so adapters
check these limits before inlining anything and route the rest elsewhere.
"""

from __future__ import annotations

import base64
import io
import threading
from typing import Literal

from anthropic.types.beta.sessions import (
    BetaManagedAgentsBase64ImageSourceParam,
    BetaManagedAgentsImageBlockParam,
)
from PIL import Image, ImageOps

VisionMediaType = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]

# Media types the API accepts as image blocks. Anything else (image/svg+xml,
# image/avif, ...) is rejected and would fail the whole user.message event.
VISION_MEDIA_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)

# Per-image API cap (5MB decoded).
MAX_VISION_IMAGE_BYTES: int = 5 * 1024 * 1024

# Largest image fetched to be downscaled under MAX_VISION_IMAGE_BYTES; past it, a link.
MAX_DOWNSCALE_SOURCE_BYTES: int = 25 * 1024 * 1024
# Decoding plus copying a 13000px square took about 2 GB: past this, no downscale.
_MAX_DOWNSCALE_PIXELS = 40_000_000
# Longest edge of a downscaled copy; the API resizes anything larger anyway.
_DOWNSCALE_EDGE = 2000
_QUALITIES = (85, 70, 55)
_FORMATS = ("PNG", "JPEG", "GIF", "WEBP")
_MEDIA_TYPES: dict[str, VisionMediaType] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}
# Decoding is the memory peak (hundreds of MB for a large image), and downscales run on
# worker threads: at most two at once however many images arrive.
_DECODE_SLOTS = threading.BoundedSemaphore(2)

# Per-image pixel cap: the API rejects any image edge over 8000px ("image
# dimensions exceed"). A compressed PNG/WebP can sit well under the byte cap
# yet blow past this, so dimensions are a separate gate.
MAX_VISION_IMAGE_DIMENSION: int = 8000

# Per-turn image cap. Past 20 images in one request the API tightens the
# per-image edge limit from 8000px to 2000px, so overflow is skipped instead.
MAX_VISION_IMAGES: int = 20


def sniff_image_media_type(data: bytes) -> VisionMediaType | None:
    """Media type from the image's magic bytes, or None if unrecognized.

    A platform's declared type can disagree with the bytes (a PNG labeled
    ``image/webp``). The API validates the declared type against the bytes,
    and a mismatch replayed on every later turn kills the session, so the
    bytes are the truth.
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


def build_image_block(data: bytes, media_type: VisionMediaType) -> BetaManagedAgentsImageBlockParam:
    """A base64 image content block for a ``user.message`` event."""
    return BetaManagedAgentsImageBlockParam(
        type="image",
        source=BetaManagedAgentsBase64ImageSourceParam(
            type="base64", media_type=media_type, data=base64.standard_b64encode(data).decode()
        ),
    )


# What an adapter tells the agent when `fit_vision_image` gives up on an image.
UNFIT_REASON = (
    "could not be read or resized to fit the model's image limits "
    f"({MAX_VISION_IMAGE_BYTES // (1024 * 1024)} MiB, {MAX_VISION_IMAGE_DIMENSION}px on an edge)"
)


def resized_for_vision(size: int, width: int | None, height: int | None) -> bool:
    """Whether `fit_vision_image` shrinks an image of this byte size and (known) pixels."""
    wide = (
        width is not None and height is not None and max(width, height) > MAX_VISION_IMAGE_DIMENSION
    )
    return size > MAX_VISION_IMAGE_BYTES or wide


def fit_vision_image(data: bytes) -> tuple[bytes, VisionMediaType] | None:
    """The image as a vision block may carry it: as is when within the byte and edge caps,
    else a downscaled copy. None when it is not a readable PNG, JPEG, GIF or WebP, or is
    too big to resize. Every adapter applies this one rule. CPU-bound: run it off the loop."""
    media_type = sniff_image_media_type(data)
    if media_type is None:
        return None
    try:
        with Image.open(io.BytesIO(data), formats=_FORMATS) as probe:
            width, height = probe.size
    except Exception:  # any decoder failure means "not a usable image"
        return None
    if len(data) <= MAX_VISION_IMAGE_BYTES and max(width, height) <= MAX_VISION_IMAGE_DIMENSION:
        return data, media_type
    return downscale_image(data)


def downscale_image(data: bytes) -> tuple[bytes, VisionMediaType] | None:
    """A copy of an image too big for a vision block, re-encoded under the byte cap.

    Still images only (an animated GIF keeps its first frame). Orientation is applied
    and the colour profile kept; transparency stays (PNG, else WebP), otherwise JPEG.
    None when the bytes are not a readable image, are too large to decode, or will not
    fit. A corrupt image can fail anywhere in decoding (`SyntaxError`, `struct.error`,
    `EOFError`, `MemoryError`), so every failure is None and callers fall back to a link.
    CPU-bound: run it off the event loop.
    """
    try:
        with _DECODE_SLOTS, Image.open(io.BytesIO(data), formats=_FORMATS) as source:
            if source.format == "JPEG":
                # Decodes at up to 1/8 scale, so the pixel cap applies to that size.
                source.draft("RGB", (_DOWNSCALE_EDGE, _DOWNSCALE_EDGE))
            if source.width * source.height > _MAX_DOWNSCALE_PIXELS:
                return None
            # A CMYK or greyscale profile would mislabel the RGB copy, so only RGB-like keep it.
            rgb_like = source.mode in ("RGB", "RGBA", "P", "PA")
            profile = source.info.get("icc_profile") if rgb_like else None
            image: Image.Image = source
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if image.has_transparency_data else "RGB")
            image.thumbnail((_DOWNSCALE_EDGE, _DOWNSCALE_EDGE))
            image = ImageOps.exif_transpose(image)
        if image.mode == "RGBA" and image.getchannel("A").getextrema()[0] == 255:
            image = image.convert("RGB")  # an alpha channel that hides nothing
        return _encode(image, profile)
    except Exception:  # corrupt input raises many types; None is the fallback
        return None


def _encode(image: Image.Image, profile: object) -> tuple[bytes, VisionMediaType] | None:
    """The first encoding under the byte cap: JPEG, or for transparency PNG then WebP."""
    extra = {"icc_profile": profile} if isinstance(profile, bytes) else {}
    tries: list[tuple[str, dict[str, int]]] = (
        [("PNG", {}), *(("WEBP", {"quality": q}) for q in _QUALITIES)]
        if image.mode == "RGBA"
        else [("JPEG", {"quality": q}) for q in _QUALITIES]
    )
    for fmt, options in tries:
        buffer = io.BytesIO()
        image.save(buffer, format=fmt, **options, **extra)
        if buffer.tell() <= MAX_VISION_IMAGE_BYTES:
            return buffer.getvalue(), _MEDIA_TYPES[fmt]
    return None
