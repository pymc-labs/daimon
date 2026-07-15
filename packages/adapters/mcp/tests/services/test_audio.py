"""Tests for AudioService (Gemini TTS wrapper).

Responses are constructed inline as real ``google.genai.types.*`` Pydantic
models, serialized to camelCase Gemini REST JSON via ``model_dump(mode="json",
by_alias=True)``, and returned from a transport-level ``make_stub_gemini``
fake — the real SDK parses every response. See ``conftest.make_stub_gemini``.
"""

from __future__ import annotations

import httpx
import pytest
from daimon.adapters.mcp.services.audio import AudioService
from daimon.core.media.audio_script import SpeakerSegment
from google.genai import types

from .conftest import make_stub_gemini


def _audio_response(pcm: bytes, *, prompt_tokens: int, thoughts_tokens: int) -> httpx.Response:
    """Build a real GenerateContentResponse carrying one PCM Blob + usage metadata.

    Inlined at every test call site that needs an audio payload. Catches
    SDK schema drift the way the testing guideline asks for: when Blob
    or Part or Candidate gains a required field, every audio test
    constructing one will break at import time.
    """
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    parts=[types.Part(inline_data=types.Blob(data=pcm, mime_type="audio/pcm"))]
                ),
                finish_reason=types.FinishReason.STOP,
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=1,
            thoughts_token_count=thoughts_tokens,
            cached_content_token_count=0,
        ),
    )
    return httpx.Response(200, json=response.model_dump(mode="json", by_alias=True))


@pytest.mark.asyncio
async def test_generate_produces_mp3_and_sums_usage_across_segments() -> None:
    """Service synthesizes each segment then encodes the concatenation as MP3,
    returning one MediaUsage that is the sum of both segments' usage_metadata."""
    # 240 samples of silence per call (10 ms at 24 kHz / 16-bit / mono).
    pcm_silence = b"\x00\x00" * 240
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _audio_response(pcm_silence, prompt_tokens=10, thoughts_tokens=3)

    client = make_stub_gemini(handler)
    service = AudioService(client=client)
    segments = [
        SpeakerSegment(speaker="HOST_A", text="hello"),
        SpeakerSegment(speaker="HOST_B", text="world"),
    ]
    result = await service.generate(segments, {"HOST_A": "Charon", "HOST_B": "Fenrir"})
    assert result.mp3.startswith((b"\xff\xfb", b"\xff\xf3", b"ID3")), (
        f"expected MP3-framed bytes, got prefix {result.mp3[:3].hex()}"
    )
    assert len(calls) == 2, "TTS should run once per segment"
    assert result.usage.input_tokens == 20, "input_tokens should sum both segments' prompt tokens"
    assert result.usage.output_tokens == 8, (
        "output_tokens should sum candidates+thoughts across both segments (1+3 each)"
    )
    assert result.usage.cache_read_input_tokens == 0, "no cached tokens in either segment"


@pytest.mark.asyncio
async def test_synthesize_segment_raises_on_no_audio_payload() -> None:
    """An empty candidates list across all retries surfaces as RuntimeError."""

    def empty_handler(_request: httpx.Request) -> httpx.Response:
        response = types.GenerateContentResponse(candidates=[])
        return httpx.Response(200, json=response.model_dump(mode="json", by_alias=True))

    service = AudioService(client=make_stub_gemini(empty_handler))
    with pytest.raises(RuntimeError, match="Gemini TTS failed"):
        await service.synthesize_segment("hello", "Charon")
