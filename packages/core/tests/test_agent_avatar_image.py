"""Uploaded avatar normalization bounds and metadata stripping."""

from __future__ import annotations

import io

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


def test_pixel_cap_rejected_before_decode() -> None:
    image = Image.new("1", (5001, 5000))
    with pytest.raises(ValueError, match="dimensions"):
        normalize_avatar_image(_bytes(image, "PNG"))
