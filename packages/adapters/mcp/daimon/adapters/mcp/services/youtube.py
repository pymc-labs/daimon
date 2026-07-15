"""Transcribe public YouTube videos via Gemini.

Gemini supports YouTube URLs natively as multimodal video input — no
proxy, no scraping, no transcript API. We pass the URL via
``types.Part(file_data=...)`` and prompt for a timestamped transcript.

Why ``media_resolution=MEDIA_RESOLUTION_LOW``: Flash's 1M-token input
context maps to ~55 min of standard-resolution video (300 tok/sec) but
~165 min at low resolution (~100 tok/sec). For transcript extraction the
audio carries the content; the visual fidelity loss costs little while
buying ~3x capacity, covering essentially every podcast/talk people share.

Why ``max_output_tokens=65536``: when unset, Flash defaults to a much
lower ~8K output ceiling. 65,536 is Flash's documented hard ceiling and
covers ~5h30m of speech at typical pace, well beyond the 2h45m input
limit. This is a ceiling, not a quota — short videos still finish
exactly as before.

Why the abridgement clause in the prompt: on a 90-min podcast, even
with the full output ceiling available, Flash got stuck around the
20-minute mark and looped a 5-min segment dozens of times. The prompt
explicitly grants the model permission to abridge filler when a fully
verbatim transcript would be too long, giving it an external reason
to keep advancing rather than getting stuck. (A positive
frequency_penalty would be the canonical decoding-level fix, but
Gemini returns 400 INVALID_ARGUMENT — penalty parameters are not
enabled on gemini-2.5-flash.)

Note: the prompt requests ``[HH:MM:SS]`` markers but real Gemini emits
``[MM:SS:millis]`` on short videos. Any downstream regex must accept
``\\[\\d{1,3}:\\d{2}(:\\d{2,3})?\\]``.

Limits (per https://ai.google.dev/gemini-api/docs/video-understanding):
- Free tier: 8 hours of YouTube video per day.
- Paid tier: no length limit.
- Public videos only — private/unlisted return an error.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from daimon.adapters.mcp.services._usage import MediaUsage, from_metadata
from google import genai
from google.genai import errors, types

log = structlog.get_logger()

_MODEL: str = "gemini-2.5-flash"

_TRANSCRIPT_PROMPT: str = (
    "Provide a faithful transcript of this video in English. Insert an "
    "[HH:MM:SS] timestamp on its own line every ~30 seconds, or whenever "
    "the speaker or topic changes. Render the spoken words verbatim. "
    "When something significant happens visually that is not captured in "
    "the audio (e.g. a chart is shown, the speaker holds up an object, "
    "on-screen text appears), add a single bracketed note like "
    "'[shows revenue chart]' inline at the relevant moment. "
    "IMPORTANT: complete coverage from start to end of the video is "
    "non-negotiable — every speaker, segment, and topic transition must "
    "appear in the output. If the video is long and a fully verbatim "
    "transcript would exceed your output budget, you MAY abridge filler "
    "words, repetitions, and tangential asides to make room, but never "
    "drop or skip over substantive content from any speaker. "
    "Do not add a preamble or closing remarks — just the transcript."
)

_MAX_OUTPUT_TOKENS: int = 65536  # Flash's hard ceiling, ~5h30m of speech


class YouTubeTranscriptError(Exception):
    """Transcript extraction failed. Message is safe to surface to the agent."""


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    usage: MediaUsage


class YouTubeService:
    """Wraps the Gemini client for YouTube video transcription."""

    def __init__(self, client: genai.Client) -> None:
        self._client = client

    async def extract_transcript(self, url: str) -> TranscriptResult:
        """Return the timestamped transcript for a public YouTube URL.

        Raises ``YouTubeTranscriptError`` with a user-actionable message
        when Gemini rejects the request (private video, quota exhausted,
        etc.). The MCP tool wraps these into ``ToolError``.
        """
        contents = [
            types.Part(file_data=types.FileData(file_uri=url, mime_type="video/*")),
            _TRANSCRIPT_PROMPT,
        ]
        config = types.GenerateContentConfig(
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        )

        try:
            # google-genai's contents union is invariant on list[T] and partially-typed
            # for mixed Part + str inputs; both ignores are SDK-shape, not real errors.
            response = await self._client.aio.models.generate_content(  # pyright: ignore[reportUnknownMemberType]
                model=_MODEL,
                contents=contents,  # pyright: ignore[reportArgumentType]
                config=config,
            )
        except errors.ClientError as exc:
            if exc.code == 429:
                raise YouTubeTranscriptError(
                    "Gemini quota exhausted for video processing. Try again later."
                ) from exc
            raise YouTubeTranscriptError(
                f"Gemini rejected the video: {exc}. Common causes: the video "
                f"is longer than Gemini's video context window (~2h45m at low "
                f"resolution); the video is private, unlisted, or age-restricted; "
                f"the daily YouTube quota has been exceeded (8h/day on free tier)."
            ) from exc
        except errors.ServerError as exc:
            raise YouTubeTranscriptError(f"Gemini server error (retryable): {exc}") from exc

        text = (response.text or "").strip()
        if not text:
            raise YouTubeTranscriptError(
                "Gemini returned an empty transcript. The video may have no "
                "audible speech or may be unsupported."
            )

        log.info("youtube_transcript_extracted", url=url, length_chars=len(text))
        return TranscriptResult(text=text, usage=from_metadata(response.usage_metadata))
