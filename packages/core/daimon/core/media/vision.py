"""Model-API vision limits and image content blocks, shared by every chat adapter.

Pure: no I/O. An image block that fails model-API validation is replayed by
Managed Agents on every later turn and terminates the session, so adapters
check these limits before inlining anything and route the rest elsewhere.
"""

from __future__ import annotations

import base64
from typing import Literal

from anthropic.types.beta.sessions import (
    BetaManagedAgentsBase64ImageSourceParam,
    BetaManagedAgentsImageBlockParam,
)

VisionMediaType = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]

# Media types the API accepts as image blocks. Anything else (image/svg+xml,
# image/avif, ...) is rejected and would fail the whole user.message event.
VISION_MEDIA_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)

# Per-image API cap (5MB decoded).
MAX_VISION_IMAGE_BYTES: int = 5 * 1024 * 1024

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
