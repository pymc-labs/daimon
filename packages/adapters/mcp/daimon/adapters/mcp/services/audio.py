"""Gemini TTS synthesis + PCM concatenation + MP3 encoding."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass

import imageio_ffmpeg
import structlog
from daimon.adapters.mcp.services._usage import MediaUsage, from_metadata, sum_media_usage
from daimon.core.media.audio_script import SpeakerSegment
from google import genai
from google.genai import errors, types
from pydub import AudioSegment

log = structlog.get_logger()

# Configure pydub to use the bundled ffmpeg binary.
AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()

PCM_SAMPLE_RATE = 24_000
PCM_SAMPLE_WIDTH = 2  # 16-bit
PCM_CHANNELS = 1
DISCORD_FILE_SIZE_LIMIT = 24_000_000  # 24 MB (margin below Discord's 25 MB)

TTS_MODEL = "gemini-3.1-flash-tts-preview"
MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (1.0, 2.0, 4.0)

_HARM_CATEGORIES = (
    types.HarmCategory.HARM_CATEGORY_HARASSMENT,
    types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
    types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
    types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
)


@dataclass(frozen=True)
class AudioResult:
    mp3: bytes
    usage: MediaUsage


class AudioService:
    """Generates podcast audio from parsed script segments via Gemini TTS."""

    def __init__(self, client: genai.Client) -> None:
        self._client = client

    async def synthesize_segment(self, text: str, voice: str) -> tuple[bytes, MediaUsage]:
        """Call Gemini TTS for a single segment. Returns raw PCM bytes (24kHz, 16-bit, mono).

        Retries up to ``MAX_ATTEMPTS`` times with exponential backoff on:
        - ``ServerError`` (5xx)
        - ``ClientError`` with ``code == 429``
        - missing ``inline_data`` in the response
        - ``finish_reason != FinishReason.STOP``

        Fails fast on any other ``ClientError`` (non-429 4xx).
        """
        config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice),
                ),
            ),
            safety_settings=[
                types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.BLOCK_NONE)
                for c in _HARM_CATEGORIES
            ],
        )

        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                # google-genai's stubs leak an Unknown into the `contents` union
                # (one of the alternatives is partially-typed). Ignored narrowly.
                response = await self._client.aio.models.generate_content(  # pyright: ignore[reportUnknownMemberType]
                    model=TTS_MODEL,
                    contents=text,
                    config=config,
                )
            except errors.ClientError as exc:
                if exc.code != 429:
                    raise
                last_error = exc
                log.warning("audio.tts_rate_limited", attempt=attempt + 1, code=exc.code)
            except errors.ServerError as exc:
                last_error = exc
                log.warning("audio.tts_server_error", attempt=attempt + 1, code=exc.code)
            else:
                pcm = _extract_pcm(response)
                if pcm is not None:
                    return pcm, from_metadata(response.usage_metadata)
                last_error = RuntimeError("Gemini TTS returned no audio payload")
                log.warning("audio.tts_bad_response", attempt=attempt + 1)

            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_SECONDS[attempt])

        raise RuntimeError(
            f"Gemini TTS failed after {MAX_ATTEMPTS} attempts: {last_error}"
        ) from last_error

    async def generate(
        self, segments: list[SpeakerSegment], voice_map: dict[str, str]
    ) -> AudioResult:
        """Generate a complete podcast MP3 from parsed segments.

        Synthesizes each segment via Gemini TTS, concatenates PCM, encodes
        to MP3 via pydub. Returns one aggregated ``MediaUsage`` summed across
        every segment call — the tool bills one invocation, not N segments.
        A missing speaker tag in ``voice_map`` is a programmer bug
        (validate_script should have caught it) and surfaces as KeyError.
        """
        pcm_chunks: list[bytes] = []
        segment_usages: list[MediaUsage] = []
        for i, segment in enumerate(segments):
            voice = voice_map[segment.speaker]
            log.info(
                "audio.synthesize_segment",
                index=i,
                speaker=segment.speaker,
                text_len=len(segment.text),
                voice=voice,
            )
            chunk, usage = await self.synthesize_segment(segment.text, voice)
            pcm_chunks.append(chunk)
            segment_usages.append(usage)

        pcm_bytes = b"".join(pcm_chunks)
        duration_seconds = len(pcm_bytes) / (PCM_SAMPLE_RATE * PCM_SAMPLE_WIDTH * PCM_CHANNELS)
        log.info(
            "audio.encode_mp3",
            pcm_bytes=len(pcm_bytes),
            duration_seconds=round(duration_seconds, 1),
        )

        mp3_bytes = _pcm_to_mp3(pcm_bytes)
        log.info(
            "audio.generation_complete",
            mp3_bytes=len(mp3_bytes),
            segments=len(segments),
            duration_seconds=round(duration_seconds, 1),
        )
        return AudioResult(mp3=mp3_bytes, usage=sum_media_usage(segment_usages))


def _extract_pcm(response: types.GenerateContentResponse) -> bytes | None:
    """Return PCM bytes if the response is well-formed, else None (triggers retry)."""
    if not response.candidates:
        return None
    candidate = response.candidates[0]
    if candidate.finish_reason != types.FinishReason.STOP:
        return None
    content = candidate.content
    if content is None or not content.parts:
        return None
    inline = content.parts[0].inline_data
    if inline is None or inline.data is None:
        return None
    return inline.data


def _pcm_to_mp3(pcm_bytes: bytes) -> bytes:
    """Convert raw PCM bytes (24 kHz, 16-bit, mono) to MP3."""
    segment = AudioSegment(
        data=pcm_bytes,
        sample_width=PCM_SAMPLE_WIDTH,
        frame_rate=PCM_SAMPLE_RATE,
        channels=PCM_CHANNELS,
    )
    buffer = io.BytesIO()
    segment.export(buffer, format="mp3", bitrate="128k")
    return buffer.getvalue()
