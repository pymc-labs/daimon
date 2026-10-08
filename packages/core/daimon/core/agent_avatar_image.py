"""Validate and normalize an uploaded agent avatar."""

from __future__ import annotations

import io

from PIL import Image, ImageOps

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
ALLOWED_FORMATS = frozenset({"PNG", "JPEG", "MPO", "GIF", "WEBP"})


def normalize_avatar_image(data: bytes) -> bytes:
    """Return a metadata-free, center-cropped 256px PNG from one still image."""
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Upload an image of at most 2 MB.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ALLOWED_FORMATS:
                raise ValueError("Upload a PNG, JPG, GIF, or WebP image.")
            if image.format != "MPO" and getattr(image, "n_frames", 1) != 1:
                raise ValueError("Animated images are not supported.")
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("Image dimensions are too large.")
            if image.format in {"JPEG", "MPO"}:
                image.seek(0)
                image.draft("RGB", (512, 512))
            if image.mode == "I" or image.mode.startswith("I;16"):
                image = image.point(lambda value: value / 256).convert("L")
            elif image.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
                image = image.convert("RGBA")
            image.thumbnail((512, 512), Image.Resampling.LANCZOS)
            oriented = ImageOps.exif_transpose(image)
            rgba = oriented.convert("RGBA")
            cropped = ImageOps.fit(rgba, (256, 256), method=Image.Resampling.LANCZOS)
            clean = Image.new("RGB", (256, 256), "white")
            clean.paste(cropped, mask=cropped.getchannel("A"))
            output = io.BytesIO()
            clean.save(output, format="PNG", optimize=True)
            result = output.getvalue()
            if len(result) > 256 * 1024:
                raise ValueError("The normalized image is too detailed for an avatar.")
            return result
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The uploaded image could not be read.") from exc
