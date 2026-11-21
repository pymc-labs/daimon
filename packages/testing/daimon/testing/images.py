"""Real image bytes for tests: vision paths decode what they inline."""

from __future__ import annotations

import io

from PIL import Image


def png_bytes(width: int = 4, height: int = 4) -> bytes:
    """A small valid PNG."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "teal").save(buffer, "PNG")
    return buffer.getvalue()


def webp_bytes(width: int = 4, height: int = 4) -> bytes:
    """A small valid WebP."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "teal").save(buffer, "WEBP")
    return buffer.getvalue()
