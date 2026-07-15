"""Speaker-tagged podcast script parsing + speaker→voice mapping."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SpeakerSegment:
    speaker: str
    text: str


MAX_SEGMENT_CHARS = 4096

_SPEAKER_TAG_RE = re.compile(r"^\[([A-Z][A-Z0-9_]*)\]\s*", re.MULTILINE)

DEFAULT_VOICE = "Charon"

DEFAULT_VOICE_MAP: dict[str, str] = {
    "HOST_A": "Charon",
    "HOST_B": "Charon",
    "NARRATOR": "Charon",
}


def parse_script(script: str) -> list[SpeakerSegment]:
    """Parse a podcast script with [SPEAKER] prefixes into typed segments."""
    if not script.strip():
        raise ValueError("Script is empty")

    matches = list(_SPEAKER_TAG_RE.finditer(script))
    if not matches:
        raise ValueError(
            "No speaker tags found — use uppercase [TAG] prefixes like [NARRATOR] or [HOST_A]"
        )

    segments: list[SpeakerSegment] = []
    for i, match in enumerate(matches):
        speaker = match.group(1)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(script)
        text = script[start:end].strip()
        segments.append(SpeakerSegment(speaker=speaker, text=text))

    return segments


def validate_script(segments: list[SpeakerSegment], voice_map: dict[str, str]) -> None:
    """Validate a parsed script against the merged voice map.

    Mutates ``voice_map`` in place: speaker tags missing from it are
    populated with ``DEFAULT_VOICE`` so downstream synthesis can do a
    direct lookup without further guards.
    """
    if not segments:
        raise ValueError("Empty segment list")

    for i, segment in enumerate(segments):
        if not segment.text:
            raise ValueError(f"Segment {i} ({segment.speaker}) has empty text")
        if len(segment.text) > MAX_SEGMENT_CHARS:
            raise ValueError(
                f"Segment {i} ({segment.speaker}) has {len(segment.text)} chars, "
                f"exceeding the {MAX_SEGMENT_CHARS} character limit"
            )

    distinct_speakers = {s.speaker for s in segments}
    for tag in distinct_speakers:
        if tag not in voice_map:
            voice_map[tag] = DEFAULT_VOICE
