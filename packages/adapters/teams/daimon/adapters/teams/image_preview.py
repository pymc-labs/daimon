"""Inline image previews for session outputs: a small data-URI attachment Teams renders.

Teams shows a bot attachment whose `content_url` is a `data:` URI inline, but
only up to 1024x1024 px and 1 MB, PNG, JPEG or a still GIF. The SDK does not
check, and an oversize one is sent but never rendered, so everything here
re-encodes to fit or returns None.
"""

from __future__ import annotations

import base64
import io

from microsoft_teams.api import Attachment
from PIL import Image

MAX_PREVIEW_DIMENSION = 1024
# Teams' cap is 1 MB, but the base64 text rides inside the activity (4/3 the
# bytes) next to up to MAX_PREVIEWS siblings; 512 KiB keeps a full message
# near 2.7 MB, and a 1024px chart is far under it as PNG.
MAX_PREVIEW_BYTES = 512 * 1024
MAX_PREVIEWS = 4
# Pillow only warns up to 178M pixels, and decoding plus `copy()` of a 13000px
# square took about 2 GB: anything this large gets no preview.
MAX_PREVIEW_PIXELS = 40_000_000
PREVIEW_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_FORMATS = ("PNG", "JPEG", "GIF", "WEBP")
_PNG_MODES = frozenset({"1", "L", "LA", "P", "RGB", "RGBA"})
_JPEG_QUALITIES = (85, 70, 55, 40)


def _encode(image: Image.Image, image_format: str, **options: int | bool) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **options)
    return buffer.getvalue()


def _on_white(image: Image.Image) -> Image.Image:
    """JPEG has no alpha: flatten transparency onto white, as a chart's page would be."""
    rgba = image.convert("RGBA")
    flat = Image.new("RGB", rgba.size, "white")
    flat.paste(rgba, mask=rgba.getchannel("A"))
    return flat


def _fit(source: Image.Image) -> tuple[str, bytes] | None:
    lossy = source.format == "JPEG"
    if lossy:
        source.draft("RGB", (MAX_PREVIEW_DIMENSION, MAX_PREVIEW_DIMENSION))
    image = source.copy()
    if image.mode in ("1", "P") and max(image.size) > MAX_PREVIEW_DIMENSION:
        image = image.convert("RGBA")  # Palette images only downscale by nearest neighbour.
    image.thumbnail((MAX_PREVIEW_DIMENSION, MAX_PREVIEW_DIMENSION))
    # A photo is already lossy: PNG would only be larger, so it goes straight to JPEG.
    if not lossy:
        png = _encode(image if image.mode in _PNG_MODES else image.convert("RGBA"), "PNG")
        if len(png) <= MAX_PREVIEW_BYTES:
            return "image/png", png
    flat = _on_white(image)
    for quality in _JPEG_QUALITIES:
        jpeg = _encode(flat, "JPEG", quality=quality)
        if len(jpeg) <= MAX_PREVIEW_BYTES:
            return "image/jpeg", jpeg
    return None


def preview_image(content: bytes, mime_type: str) -> tuple[str, bytes] | None:
    """`(mime type, bytes)` of a still image re-encoded to Teams' inline limits, or None."""
    if mime_type not in PREVIEW_MIME_TYPES:
        return None
    try:
        with Image.open(io.BytesIO(content), formats=_FORMATS) as source:
            if source.width * source.height > MAX_PREVIEW_PIXELS:
                return None
            if getattr(source, "n_frames", 1) != 1:
                return None  # Teams does not render animated GIFs inline.
            return _fit(source)
    except (OSError, ValueError, Image.DecompressionBombError):
        # Pillow's errors for a truncated, corrupt or oversized image: no preview.
        return None


def preview_attachment(name: str, content: bytes, mime_type: str) -> Attachment | None:
    """An inline image attachment for one output, or None when it cannot be shown."""
    preview = preview_image(content, mime_type)
    if preview is None:
        return None
    preview_type, data = preview
    encoded = base64.b64encode(data).decode("ascii")
    return Attachment(
        content_type=preview_type, content_url=f"data:{preview_type};base64,{encoded}", name=name
    )
