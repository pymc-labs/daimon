"""Pure, deterministic agent-slug sanitizer for notebook publishing.

The notebook host accepts slugs matching
``^[A-Za-z0-9_][A-Za-z0-9_-]{0,31}$`` (a valid leading char then up to 31
more from the alnum/underscore/dash set). A caller-supplied slug that violates
this — path separators, dots, a leading dash, or simply too long — is repaired
into a conformant slug here rather than rejected, so slug resolution can
succeed on a best-effort basis. No I/O, no randomness: same input, same output.
"""

from __future__ import annotations

import re

AGENT_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,31}$")

_INVALID_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_FALLBACK_LEADING_CHAR = "n"
_MAX_SLUG_LEN = 32


def sanitize_slug(raw: str) -> str:
    """Return a slug matching ``AGENT_SLUG_PATTERN``, repairing ``raw`` as needed.

    Strips any character outside ``[A-Za-z0-9_-]``, drops leading dashes,
    falls back to a fixed safe leading char when nothing valid remains, and
    truncates to 32 characters.
    """
    stripped = _INVALID_CHARS.sub("", raw)
    stripped = stripped.lstrip("-")
    if not stripped:
        stripped = _FALLBACK_LEADING_CHAR
    return stripped[:_MAX_SLUG_LEN]
