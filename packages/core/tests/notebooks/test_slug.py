from __future__ import annotations

import re

from daimon.core.notebooks.slug import sanitize_slug

_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,31}$")


def test_sanitize_slug_truncates_over_long_to_at_most_32_chars() -> None:
    raw = "T7bp57qu0x96-montreal-gp-sprint-corners"
    assert len(raw) > 32, "fixture must exceed the 32-char cap"
    result = sanitize_slug(raw)
    assert len(result) <= 32, f"sanitized slug must be <=32 chars, got {len(result)}"
    assert _PATTERN.fullmatch(result), f"sanitized slug must match the pattern, got {result!r}"


def test_sanitize_slug_strips_invalid_characters() -> None:
    result = sanitize_slug("../etc/passwd")
    assert _PATTERN.fullmatch(result), f"sanitized slug must match the pattern, got {result!r}"
    assert "/" not in result and "." not in result, "path characters must be stripped"


def test_sanitize_slug_fixes_invalid_leading_char() -> None:
    result = sanitize_slug("-leading-dash")
    assert _PATTERN.fullmatch(result), f"sanitized slug must match the pattern, got {result!r}"
    assert result[0] != "-", "leading char must be valid (not a dash)"


def test_sanitize_slug_falls_back_when_stripped_to_empty() -> None:
    result = sanitize_slug("////")
    assert _PATTERN.fullmatch(result), f"empty-after-strip must yield a valid slug, got {result!r}"


def test_sanitize_slug_returns_valid_slug_unchanged() -> None:
    raw = "dashboard_v2-final"
    assert _PATTERN.fullmatch(raw), "fixture must already be valid"
    assert sanitize_slug(raw) == raw, "already-valid <=32 slug must pass through unchanged"
