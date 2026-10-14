"""Inline previews: still images re-encoded to Teams' inline limits, or nothing."""

from __future__ import annotations

import base64
import io
import random

import pytest
from daimon.adapters.teams import image_preview
from daimon.adapters.teams.image_preview import (
    MAX_PREVIEW_BYTES,
    preview_attachment,
    preview_image,
)
from PIL import Image


def _image_bytes(size: tuple[int, int], image_format: str, *, mode: str = "RGBA") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, (30, 90, 200) if mode == "RGB" else (30, 90, 200, 255)).save(
        buffer, format=image_format
    )
    return buffer.getvalue()


def _noise_png(side: int) -> bytes:
    """Incompressible pixels: PNG cannot get them under the budget, JPEG can."""
    pixels = random.Random(0).randbytes(side * side * 3)
    buffer = io.BytesIO()
    Image.frombytes("RGB", (side, side), pixels).save(buffer, format="PNG")
    return buffer.getvalue()


def _opened(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def test_preview_keeps_a_small_chart_as_png() -> None:
    """A chart already within the limits stays a lossless PNG at its own size."""
    preview = preview_image(_image_bytes((640, 480), "PNG"), "image/png")

    assert preview is not None, "a small PNG must get a preview"
    mime_type, data = preview
    assert mime_type == "image/png", "PNG stays PNG when it fits the budget"
    assert _opened(data).size == (640, 480), "no resize below the 1024px limit"


def test_preview_downscales_to_fit_1024_keeping_the_aspect_ratio() -> None:
    """Teams does not render an inline image over 1024x1024."""
    preview = preview_image(_image_bytes((3000, 1500), "WEBP"), "image/webp")

    assert preview is not None, "a large WebP must get a preview"
    assert preview[0] == "image/png", "WebP is re-encoded to a format Teams renders"
    assert _opened(preview[1]).size == (1024, 512), "longest side 1024, aspect kept"


def test_preview_falls_back_to_jpeg_when_png_is_over_the_budget() -> None:
    """Photo-like pixels step down to a JPEG quality that fits."""
    content = _noise_png(1024)
    assert len(content) > MAX_PREVIEW_BYTES, "precondition: the PNG itself is too big"

    preview = preview_image(content, "image/png")

    assert preview is not None, "a JPEG step should fit the budget"
    assert preview[0] == "image/jpeg", "over-budget PNG falls back to JPEG"
    assert len(preview[1]) <= MAX_PREVIEW_BYTES, "the preview respects the byte budget"


def test_preview_sends_a_jpeg_source_as_jpeg() -> None:
    """A JPEG never round-trips through PNG, which would only be larger."""
    preview = preview_image(_image_bytes((800, 600), "JPEG", mode="RGB"), "image/jpeg")

    assert preview is not None and preview[0] == "image/jpeg", "JPEG stays JPEG"


def test_preview_is_skipped_when_nothing_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    """An oversize inline image sends but never renders, so none is better."""
    monkeypatch.setattr(image_preview, "MAX_PREVIEW_BYTES", 64)

    assert preview_image(_noise_png(256), "image/png") is None, "no step fits 64 bytes"


def test_preview_skips_an_animated_gif() -> None:
    """Teams does not render animated GIFs inline."""
    frames = [Image.new("RGB", (32, 32), color) for color in ("red", "blue")]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:])

    assert preview_image(buffer.getvalue(), "image/gif") is None, "multi-frame GIF skipped"


@pytest.mark.parametrize(
    ("content", "mime_type"),
    [(b"a,b\n", "text/csv"), (b"not an image", "image/png"), (b"%PDF-1.7", "image/png")],
)
def test_preview_skips_non_images_and_corrupt_bytes(content: bytes, mime_type: str) -> None:
    """Anything Pillow cannot open as a still image gets no preview, and no error."""
    assert preview_image(content, mime_type) is None, "no preview for unreadable content"


def test_preview_attachment_is_a_data_uri_image() -> None:
    """Teams renders an image attachment whose content URL is a base64 data URI."""
    attachment = preview_attachment("chart.png", _image_bytes((10, 10), "PNG"), "image/png")

    assert attachment is not None, "a PNG gets an attachment"
    assert (attachment.content_type, attachment.name) == ("image/png", "chart.png")
    prefix = "data:image/png;base64,"
    assert attachment.content_url and attachment.content_url.startswith(prefix), "data URI"
    decoded = base64.b64decode(attachment.content_url.removeprefix(prefix))
    assert _opened(decoded).size == (10, 10), "the payload is the encoded preview"
