"""Uploaded avatar normalization bounds and metadata stripping."""

from __future__ import annotations

import io
import struct
from unittest.mock import patch

import pytest
from daimon.core.agent_avatar_image import MAX_UPLOAD_BYTES, normalize_avatar_image
from PIL import Image, PngImagePlugin


def _bytes(image: Image.Image, fmt: str, **kwargs: object) -> bytes:
    output = io.BytesIO()
    image.save(output, format=fmt, **kwargs)
    return output.getvalue()


def test_center_crop_and_metadata_removed() -> None:
    source = Image.new("RGB", (400, 200), "red")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "private")
    normalized = normalize_avatar_image(_bytes(source, "PNG", pnginfo=metadata))
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.format == "PNG"
        assert image.size == (256, 256)
        assert "Comment" not in image.info


def test_exif_orientation_removed() -> None:
    image = Image.new("RGB", (100, 200), "blue")
    exif = Image.Exif()
    exif[274] = 6
    normalized = normalize_avatar_image(_bytes(image, "JPEG", exif=exif))
    with Image.open(io.BytesIO(normalized)) as result:
        assert result.size == (256, 256)
        assert "exif" not in result.info


def test_two_frame_mpo_uses_first_jpeg() -> None:
    first = Image.new("RGB", (300, 300), "red")
    second = Image.new("RGB", (300, 300), "blue")
    output = io.BytesIO()
    first.save(output, format="MPO", save_all=True, append_images=[second])
    with Image.open(io.BytesIO(output.getvalue())) as source:
        assert source.format == "MPO" and source.n_frames == 2
    normalized = normalize_avatar_image(output.getvalue())
    with Image.open(io.BytesIO(normalized)) as result:
        assert result.getpixel((128, 128))[0] > 200
        assert result.getpixel((128, 128))[2] < 50


@pytest.mark.parametrize(("value", "expected"), [(1000, 3), (32768, 128), (65535, 255)])
def test_sixteen_bit_grayscale_png_is_normalized(value: int, expected: int) -> None:
    source = Image.new("I;16", (320, 320), value)
    normalized = normalize_avatar_image(_bytes(source, "PNG"))
    with Image.open(io.BytesIO(normalized)) as result:
        assert result.mode == "RGB"
        assert result.size == (256, 256)
        assert result.getpixel((128, 128)) == (expected, expected, expected)


@pytest.mark.parametrize("error", [SyntaxError("broken PNG file"), struct.error("bad EXIF")])
def test_decoder_errors_are_value_errors(error: Exception) -> None:
    with (
        patch("daimon.core.agent_avatar_image.ImageOps.exif_transpose", side_effect=error),
        pytest.raises(ValueError, match="could not be read"),
    ):
        normalize_avatar_image(_bytes(Image.new("RGB", (32, 32)), "PNG"))


@pytest.mark.parametrize("body", [b"not an image", b"x" * (MAX_UPLOAD_BYTES + 1)])
def test_corrupt_or_oversize_bytes_rejected(body: bytes) -> None:
    with pytest.raises(ValueError):
        normalize_avatar_image(body)


def test_animated_image_rejected() -> None:
    output = io.BytesIO()
    frames = [Image.new("RGB", (20, 20), colour) for colour in ("red", "blue")]
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=50)
    with pytest.raises(ValueError, match="Animated"):
        normalize_avatar_image(output.getvalue())


def test_empty_image_is_a_read_error() -> None:
    with pytest.raises(ValueError, match="could not be read"):
        normalize_avatar_image(b"")


def test_pixel_cap_rejected_before_decode() -> None:
    image = Image.new("1", (5001, 5000))
    with pytest.raises(ValueError, match="dimensions"):
        normalize_avatar_image(_bytes(image, "PNG"))
