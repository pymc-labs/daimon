"""Pure filename derivation for files delivered as chat attachments.

No I/O. The extension matters because chat clients pick their preview from it,
so it is derived from the declared mime type rather than trusted from the
agent-supplied title.
"""

from __future__ import annotations

import mimetypes
import re

_TITLE_SLUG = re.compile(r"[^A-Za-z0-9._-]+")

# Used when mimetypes cannot decide — better an opaque extension than a wrong one.
_FALLBACK_EXT = ".bin"

# Mime types that carry no format information the title does not already have.
# An agent uploading a parquet file or a JSONL log has no accurate mime to
# declare, and deriving from what it guesses is how `orders.parquet` became
# `orders.parquet.bin` and `changelog.jsonl` became `changelog.jsonl.json`.
#
# Two ways a mime ends up uninformative, and BOTH must be caught -- an earlier
# fix listed only the first, and live turns immediately produced the second:
#   1. it is a known catch-all (`application/octet-stream`, `application/json`)
#   2. `mimetypes` has never heard of it, so it resolves to the fallback. Agents
#      invent plausible types -- `application/jsonl` was observed on staging --
#      and an invented type is no more informative than octet-stream.
# In both cases the title's own extension is the more specific fact, so it wins.
# A mime resolving to a REAL extension still overrides the title, which is the
# point of deriving at all: a PNG titled `report.pdf` must not preview as a PDF.
_CATCH_ALL_MIMES = frozenset(
    {
        "application/octet-stream",
        "application/json",
        "text/plain",
        "binary/octet-stream",
    }
)

_HAS_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def extension_for_mime(mime_type: str) -> str:
    """Return the file extension a chat client should see for ``mime_type``."""
    ext = mimetypes.guess_extension(mime_type, strict=False)
    if ext in (None, ""):
        # mimetypes has no mapping for image/jpeg on some platforms, and ".jpe"
        # on others; neither is what a user expects to download.
        if mime_type == "image/jpeg":
            return ".jpg"
        return _FALLBACK_EXT
    if ext == ".jpe":
        return ".jpg"
    return ext


def sanitize_title(title: str) -> str:
    """Reduce an agent-supplied title to a safe filename stem.

    Everything outside ``[A-Za-z0-9._-]`` collapses to an underscore, which is
    also what stops a title from carrying a path separator.
    """
    cleaned = _TITLE_SLUG.sub("_", title).strip("._-")
    return cleaned or "file"


def display_filename_for(title: str, mime_type: str) -> str:
    """Build the user-visible filename for an agent-supplied title.

    The extension is appended only when the title does not already end in it —
    an agent that helpfully passes "chart.jpg" should not get "chart.jpg.jpg" —
    and never when the mime carries no more information than the title already
    does (see :data:`_CATCH_ALL_MIMES`).
    """
    cleaned = sanitize_title(title)
    ext = extension_for_mime(mime_type)
    if cleaned.lower().endswith(ext.lower()):
        return cleaned
    uninformative = (
        mime_type.split(";")[0].strip().lower() in _CATCH_ALL_MIMES or ext == _FALLBACK_EXT
    )
    if uninformative and _HAS_EXTENSION.search(cleaned):
        return cleaned
    return f"{cleaned}{ext}"
