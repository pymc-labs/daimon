"""Tests for daimon.core.media.audio_script — script parser, voice map, validation."""

from __future__ import annotations

import pytest
from daimon.core.media.audio_script import (
    DEFAULT_VOICE,
    DEFAULT_VOICE_MAP,
    MAX_SEGMENT_CHARS,
    SpeakerSegment,
    parse_script,
    validate_script,
)


def test_parse_script_extracts_speaker_segments() -> None:
    script = "[NARRATOR] Welcome.\n[HOST_A] Hello there."
    segments = parse_script(script)
    assert segments == [
        SpeakerSegment(speaker="NARRATOR", text="Welcome."),
        SpeakerSegment(speaker="HOST_A", text="Hello there."),
    ], "parser should return one segment per tag with trimmed text"


def test_parse_script_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_script("   \n  ")


def test_parse_script_rejects_input_with_no_speaker_tags() -> None:
    with pytest.raises(ValueError, match="No speaker tags"):
        parse_script("just plain text with no tags")


def test_validate_script_fills_missing_voices_with_default() -> None:
    segments = [SpeakerSegment(speaker="NEW_SPEAKER", text="hi")]
    voice_map: dict[str, str] = {}
    validate_script(segments, voice_map)
    assert voice_map["NEW_SPEAKER"] == DEFAULT_VOICE, (
        "validator should populate voice_map with DEFAULT_VOICE for unknown speakers"
    )


def test_validate_script_rejects_empty_text() -> None:
    segments = [SpeakerSegment(speaker="X", text="")]
    with pytest.raises(ValueError, match="empty text"):
        validate_script(segments, {"X": "Charon"})


def test_validate_script_rejects_oversize_segment() -> None:
    big = "a" * (MAX_SEGMENT_CHARS + 1)
    segments = [SpeakerSegment(speaker="X", text=big)]
    with pytest.raises(ValueError, match="character limit"):
        validate_script(segments, {"X": "Charon"})


def test_default_voice_map_has_known_speakers() -> None:
    assert "HOST_A" in DEFAULT_VOICE_MAP
    assert "HOST_B" in DEFAULT_VOICE_MAP
    assert "NARRATOR" in DEFAULT_VOICE_MAP
