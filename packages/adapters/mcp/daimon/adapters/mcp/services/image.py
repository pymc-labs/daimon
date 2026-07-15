"""Gemini image generation."""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from daimon.adapters.mcp.services._usage import MediaUsage, from_metadata
from google import genai
from google.genai import types

log = structlog.get_logger()

IMAGE_MODEL = "gemini-3-pro-image-preview"


class ImageGenerationRefused(Exception):
    """Gemini refused to generate the image (safety filters, content policy)."""


@dataclass(frozen=True)
class ImageResult:
    data: bytes
    mime_type: str
    file_extension: str
    usage: MediaUsage


def _detect_format(data: bytes) -> tuple[str, str]:
    """Detect image format from magic bytes. Returns (mime_type, extension)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg", "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return "image/png", "png"


class ImageService:
    """Generates images via Gemini 3 Pro Image."""

    def __init__(self, client: genai.Client) -> None:
        self._client = client

    async def generate(
        self,
        prompt: str,
        aspect_ratio: str = "1:1",
    ) -> ImageResult:
        """Generate an image from a text prompt.

        Raises:
            ImageGenerationRefused: Gemini safety filters blocked the request.
            google.genai.errors.ClientError: 4xx API error.
            google.genai.errors.ServerError: 5xx API error.
        """
        config = types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
        )

        # google-genai's stubs leak an Unknown into the `contents` union.
        response = await self._client.aio.models.generate_content(  # pyright: ignore[reportUnknownMemberType]
            model=IMAGE_MODEL,
            contents=prompt,
            config=config,
        )

        image_data = _extract_image_data(response)
        if image_data is None:
            raise ImageGenerationRefused("Gemini returned no image data (likely safety refusal)")

        mime_type, extension = _detect_format(image_data)
        log.info(
            "image.generation_complete",
            size_bytes=len(image_data),
            mime_type=mime_type,
        )
        return ImageResult(
            data=image_data,
            mime_type=mime_type,
            file_extension=extension,
            usage=from_metadata(response.usage_metadata),
        )


def _extract_image_data(response: types.GenerateContentResponse) -> bytes | None:
    """Extract image bytes from the Gemini response, or None if no image found."""
    if not response.candidates:
        return None
    candidate = response.candidates[0]
    content = candidate.content
    if content is None or not content.parts:
        return None
    for part in content.parts:
        if part.inline_data is not None and part.inline_data.data is not None:
            return part.inline_data.data
    return None
