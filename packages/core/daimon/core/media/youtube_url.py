"""Validate and parse YouTube URLs."""

from __future__ import annotations

import re

_VIDEO_ID_RE: re.Pattern[str] = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|live/|shorts/)|youtu\.be/)"
    r"([0-9A-Za-z_-]{11})"
    r"(?![0-9A-Za-z_-])"
)


def extract_video_id(url: str) -> str | None:
    """Return the 11-char YouTube video ID, or None if not a YouTube URL.

    Matches against canonical youtube.com and youtu.be hosts. Trailing
    query/fragment after the ID is tolerated. Case-sensitive on the ID
    itself (YouTube IDs preserve case).
    """
    match = _VIDEO_ID_RE.search(url)
    return match.group(1) if match else None
