"""Tests for YouTubeService (Gemini video transcript wrapper).

Responses are real ``google.genai.types.GenerateContentResponse`` instances
serialized to camelCase Gemini REST JSON and returned from a
transport-level ``make_stub_gemini`` fake — ``response.text`` is a derived
property over the first candidate's text parts, so we build the response
that way to exercise the real accessor path the service uses.
"""

from __future__ import annotations

import httpx
import pytest
from daimon.adapters.mcp.services.youtube import YouTubeService, YouTubeTranscriptError
from google.genai import types

from .conftest import make_stub_gemini


def _text_response(
    text: str, *, prompt_tokens: int = 10, thoughts_tokens: int = 4
) -> httpx.Response:
    """Build a real GenerateContentResponse with one text Part + usage metadata."""
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(parts=[types.Part(text=text)]),
                finish_reason=types.FinishReason.STOP,
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=len(text.split()),
            thoughts_token_count=thoughts_tokens,
            cached_content_token_count=0,
        ),
    )
    return httpx.Response(200, json=response.model_dump(mode="json", by_alias=True))


@pytest.mark.asyncio
async def test_extract_transcript_returns_text_and_usage() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _text_response("[00:00:00] hello world")

    service = YouTubeService(client=make_stub_gemini(handler))
    result = await service.extract_transcript("https://youtu.be/dQw4w9WgXcQ")
    assert "hello world" in result.text
    assert result.usage.input_tokens == 10, "input_tokens should map prompt_token_count"
    assert result.usage.output_tokens == 7, (
        "output_tokens should fold thoughts into candidates (3 words + 4 thoughts)"
    )


@pytest.mark.asyncio
async def test_extract_transcript_raises_on_empty_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        # Empty candidates → response.text is None → service raises.
        response = types.GenerateContentResponse(candidates=[])
        return httpx.Response(200, json=response.model_dump(mode="json", by_alias=True))

    service = YouTubeService(client=make_stub_gemini(handler))
    with pytest.raises(YouTubeTranscriptError, match="empty transcript"):
        await service.extract_transcript("https://youtu.be/dQw4w9WgXcQ")
