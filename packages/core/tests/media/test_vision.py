"""`fit_vision_image` / `downscale_image`: one rule for every adapter's vision blocks."""

from __future__ import annotations

import io
import os
import struct

import pytest
from daimon.core.media.vision import (
    MAX_VISION_IMAGE_BYTES,
    MAX_VISION_IMAGE_DIMENSION,
    downscale_image,
    fit_vision_image,
)
from PIL import Image


def _encoded(image: Image.Image, fmt: str, **options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, fmt, **options)
    return buffer.getvalue()


def _opened(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


@pytest.mark.parametrize("error", [SyntaxError, struct.error, EOFError, MemoryError])
def test_any_decoder_failure_is_none_so_the_caller_falls_back_to_a_link(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """A corrupt image past its header raises these from Pillow; none may escape a turn."""

    def broken(*_: object, **__: object) -> None:
        raise error("corrupt")

    big = _encoded(Image.new("RGB", (MAX_VISION_IMAGE_DIMENSION + 1, 8), "red"), "PNG")
    monkeypatch.setattr(Image.Image, "thumbnail", broken)
    assert downscale_image(big) is None, f"{error.__name__} is contained"
    assert fit_vision_image(big) is None, "and so is the shared rule"


def test_a_truncated_png_is_none() -> None:
    noise = _encoded(Image.frombytes("RGB", (600, 600), os.urandom(600 * 600 * 3)), "PNG")
    assert downscale_image(noise[: len(noise) // 2]) is None, "half a PNG never becomes a block"
    assert fit_vision_image(b"\x89PNG\r\n\x1a\ngarbage") is None, "a bad header is refused too"


def test_an_exif_orientation_is_applied_before_resizing() -> None:
    """A phone photo stored sideways is shown upright: the copy swaps its edges and drops the tag."""
    exif = Image.Exif()
    exif[0x0112] = 6
    sideways = _encoded(Image.new("RGB", (400, 200), "teal"), "JPEG", exif=exif)
    out = downscale_image(sideways)
    assert out is not None
    image = _opened(out[0])
    assert image.size == (200, 400), "orientation 6 turns a 400x200 frame upright"
    assert 0x0112 not in image.getexif(), "the tag is gone, so nothing turns it twice"


def test_the_colour_profile_is_kept() -> None:
    profile = bytes(range(200))  # opaque to the resize; only its survival matters
    source = _encoded(Image.new("RGB", (300, 300), "orange"), "PNG", icc_profile=profile)
    out = downscale_image(source)
    assert out is not None
    assert _opened(out[0]).info.get("icc_profile") == profile, "colours do not shift"


def test_transparency_survives_as_png_or_webp_not_a_white_box() -> None:
    source = Image.new("RGBA", (300, 300), (255, 0, 0, 255))
    source.putpixel((0, 0), (0, 0, 255, 0))
    out = downscale_image(_encoded(source, "PNG"))
    assert out is not None
    data, media_type = out
    assert media_type in ("image/png", "image/webp"), media_type
    assert _opened(data).convert("RGBA").getchannel("A").getpixel((0, 0)) == 0, (
        "the clear corner stays clear"
    )
    assert len(data) <= MAX_VISION_IMAGE_BYTES, "and it still fits"


def test_an_opaque_rgba_image_is_jpeg() -> None:
    out = downscale_image(_encoded(Image.new("RGBA", (300, 300), (9, 9, 9, 255)), "PNG"))
    assert out is not None and out[1] == "image/jpeg", (
        "an alpha channel that hides nothing is dropped"
    )


def test_an_image_within_both_caps_is_unchanged_and_one_past_the_edge_cap_is_resized() -> None:
    fine = _encoded(Image.new("RGB", (MAX_VISION_IMAGE_DIMENSION, 10), "blue"), "PNG")
    assert fit_vision_image(fine) == (fine, "image/png"), "within caps: the same bytes"
    wide = _encoded(Image.new("RGB", (MAX_VISION_IMAGE_DIMENSION + 1, 10), "blue"), "PNG")
    out = fit_vision_image(wide)
    assert out is not None
    assert max(_opened(out[0]).size) <= MAX_VISION_IMAGE_DIMENSION, "an over-edge image is shrunk"
