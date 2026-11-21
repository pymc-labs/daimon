"""Model-API vision limits and image content blocks, shared by every chat adapter.

Pure: no I/O. An image block that fails model-API validation is replayed by
Managed Agents on every later turn and terminates the session, so adapters
check these limits before inlining anything and route the rest elsewhere.
"""

from __future__ import annotations

import base64
import io
from typing import Literal

from anthropic.types.beta.sessions import (
    BetaManagedAgentsBase64ImageSourceParam,
    BetaManagedAgentsImageBlockParam,
)
from PIL import Image

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
_JPEG_QUALITIES = (85, 70, 55)

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


def downscale_image(data: bytes) -> tuple[bytes, VisionMediaType] | None:
    """A copy of an image too big for a vision block, re-encoded under the byte cap.

    Still images only (an animated GIF keeps its first frame). None when the
    bytes are not a readable PNG, JPEG, GIF or WebP, or are too large to decode.
    CPU-bound: run it off the event loop.
    """
    try:
        with Image.open(io.BytesIO(data), formats=("PNG", "JPEG", "GIF", "WEBP")) as source:
            if source.format == "JPEG":
                # Decodes at up to 1/8 scale, so the pixel cap applies to that size.
                source.draft("RGB", (_DOWNSCALE_EDGE, _DOWNSCALE_EDGE))
            if source.width * source.height > _MAX_DOWNSCALE_PIXELS:
                return None
            image = source.convert("RGBA")
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    image.thumbnail((_DOWNSCALE_EDGE, _DOWNSCALE_EDGE))
    flat = Image.new("RGB", image.size, "white")
    flat.paste(image, mask=image.getchannel("A"))
    for quality in _JPEG_QUALITIES:
        buffer = io.BytesIO()
        flat.save(buffer, format="JPEG", quality=quality)
        if buffer.tell() <= MAX_VISION_IMAGE_BYTES:
            return buffer.getvalue(), "image/jpeg"
    return None
