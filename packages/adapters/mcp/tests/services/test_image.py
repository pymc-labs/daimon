"""Tests for ImageService (Gemini 3 Pro Image wrapper).

Responses are constructed inline as real ``google.genai.types.*`` Pydantic
models, serialized to camelCase Gemini REST JSON, and returned from a
transport-level ``make_stub_gemini`` fake so SDK signature drift breaks
tests at the boundary.
"""

from __future__ import annotations

import httpx
import pytest
from daimon.adapters.mcp.services.image import ImageGenerationRefused, ImageService
from google.genai import types

from .conftest import make_stub_gemini


def _image_response(payload: bytes, mime_type: str) -> httpx.Response:
    """Build a real GenerateContentResponse carrying one image Blob + usage metadata."""
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    parts=[types.Part(inline_data=types.Blob(data=payload, mime_type=mime_type))]
                ),
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=5,
            candidates_token_count=200,
            thoughts_token_count=11,
            cached_content_token_count=0,
        ),
    )
    return httpx.Response(200, json=response.model_dump(mode="json", by_alias=True))


@pytest.mark.asyncio
async def test_generate_returns_png_for_png_magic_bytes() -> None:
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

    def handler(_request: httpx.Request) -> httpx.Response:
        return _image_response(png_bytes, mime_type="image/png")

    service = ImageService(client=make_stub_gemini(handler))
    result = await service.generate(prompt="a cat", aspect_ratio="1:1")
    assert result.data == png_bytes
    assert result.mime_type == "image/png"
    assert result.file_extension == "png"
    assert result.usage.input_tokens == 5, "input_tokens should map prompt_token_count"
    assert result.usage.output_tokens == 211, (
        "output_tokens should fold thoughts into candidates (200+11)"
    )


@pytest.mark.asyncio
async def test_generate_returns_jpeg_for_jpeg_magic_bytes() -> None:
    """Real Gemini returns JPEG today, not PNG."""
    jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100

    def handler(_request: httpx.Request) -> httpx.Response:
        return _image_response(jpeg_bytes, mime_type="image/jpeg")

    service = ImageService(client=make_stub_gemini(handler))
    result = await service.generate(prompt="a sunset", aspect_ratio="16:9")
    assert result.mime_type == "image/jpeg"
    assert result.file_extension == "jpg"


@pytest.mark.asyncio
async def test_generate_raises_refused_when_gemini_returns_no_image() -> None:
    def empty(_request: httpx.Request) -> httpx.Response:
        response = types.GenerateContentResponse(candidates=[])
        return httpx.Response(200, json=response.model_dump(mode="json", by_alias=True))

    service = ImageService(client=make_stub_gemini(empty))
    with pytest.raises(ImageGenerationRefused):
        await service.generate(prompt="...", aspect_ratio="1:1")
