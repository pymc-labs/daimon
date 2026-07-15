"""Contract tests for the three Gemini-backed services.

Env-gated and marked ``contract`` — not part of the default CI run. Set
``DAIMON_TEST_GEMINI_API_KEY`` and run with ``-m contract`` to exercise
the live API. The role of this file is to catch request-routing drift
that validated SDK-model construction in the unit tests can't detect.
Each test asserts only the response shape, not the content — Gemini's
outputs are non-deterministic.
"""

from __future__ import annotations

import os

import pytest
from daimon.adapters.mcp.services.audio import AudioService
from daimon.adapters.mcp.services.image import ImageService
from daimon.adapters.mcp.services.youtube import YouTubeService
from daimon.core.media.audio_script import SpeakerSegment
from google import genai

pytestmark = pytest.mark.contract


def _api_key() -> str:
    key = os.environ.get("DAIMON_TEST_GEMINI_API_KEY")
    if not key:
        pytest.skip("DAIMON_TEST_GEMINI_API_KEY not set — Gemini contract tests skipped")
    return key


@pytest.fixture
def gemini_client() -> genai.Client:
    return genai.Client(api_key=_api_key())


@pytest.mark.asyncio
async def test_audio_service_generates_real_mp3(gemini_client: genai.Client) -> None:
    """Live Gemini TTS produces MP3-framed bytes for a single short segment."""
    service = AudioService(client=gemini_client)
    result = await service.generate(
        [SpeakerSegment(speaker="HOST_A", text="Hello from the contract test.")],
        {"HOST_A": "Charon"},
    )
    assert result.mp3.startswith((b"\xff\xfb", b"\xff\xf3", b"ID3")), (
        f"expected MP3-framed bytes, got prefix {result.mp3[:3].hex()}"
    )


@pytest.mark.asyncio
async def test_image_service_generates_real_image_bytes(
    gemini_client: genai.Client,
) -> None:
    """Live Gemini image gen returns PNG or JPEG bytes with sane mime type."""
    service = ImageService(client=gemini_client)
    result = await service.generate(prompt="a small red dot", aspect_ratio="1:1")
    assert result.mime_type in {"image/png", "image/jpeg"}, f"unexpected mime {result.mime_type!r}"
    assert len(result.data) > 100, "expected non-trivial image payload"


@pytest.mark.asyncio
async def test_youtube_service_returns_non_empty_transcript(
    gemini_client: genai.Client,
) -> None:
    """Live YouTube transcript extraction returns non-empty text for a stable URL."""
    service = YouTubeService(client=gemini_client)
    # Rick-roll has been on YouTube > a decade and is unlikely to vanish; pick
    # a different stable video if it ever does.
    result = await service.extract_transcript("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert result.text.strip(), "expected non-empty transcript from a public video"
