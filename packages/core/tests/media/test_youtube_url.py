"""Tests for daimon.core.media.youtube_url — URL validation + ID extraction."""

from __future__ import annotations

import pytest
from daimon.core.media.youtube_url import extract_video_id


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
        "https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
    ],
)
def test_extract_video_id_recognises_canonical_shapes(url: str) -> None:
    assert extract_video_id(url) == "dQw4w9WgXcQ", f"should extract id from {url}"


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/12345",
        "https://example.com/watch?v=dQw4w9WgXcQ",
        "not a url at all",
        "https://www.youtube.com/",
        "",
    ],
)
def test_extract_video_id_returns_none_for_non_youtube(url: str) -> None:
    assert extract_video_id(url) is None, f"should return None for {url!r}"
