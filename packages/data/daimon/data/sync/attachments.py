"""Text read from the files attached to a record.

The bytes are never kept: a connector hands them over while it syncs, and only the words
taken from them are chunked with the record. Office files are parsed in a sandboxed child.
"""

from __future__ import annotations

from typing import Final

from daimon.data.sync.office import DOCX, PPTX, TEXT_CAP, XLSX
from daimon.data.sync.office_sandbox import office_text

BLOB_CAP: Final = 20_000_000
"""Bytes of one file the engine reads text from; a larger one is listed, not read."""
_OFFICE: Final = {"docx": DOCX, "xlsx": XLSX, "pptx": PPTX}
_TEXT_TYPES: Final = {
    "application/json",
    "application/xml",
    "application/csv",
    "application/x-yaml",
    "application/yaml",
}
_TEXT_EXT: Final = {"txt", "md", "markdown", "csv", "tsv", "json", "xml", "yaml", "yml", "log"}
_GENERIC: Final = ("", "application/octet-stream")


async def text_of(name: str, content_type: str, data: bytes) -> str | None:
    """The text of a text, Markdown, CSV, JSON, XML or Office file, capped at `TEXT_CAP`
    characters; None for PDFs and other binaries, and for a file that is too large."""
    if len(data) > BLOB_CAP:
        return None
    kind = (content_type or "").split(";")[0].strip().lower()
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    office = (
        kind
        if kind in (DOCX, XLSX, PPTX)
        else _OFFICE.get(ext)
        if kind in (*_GENERIC, "application/zip")
        else None
    )
    if office:
        return ((await office_text(office, data)) or "")[:TEXT_CAP] or None
    if (
        kind.startswith("text/")
        or kind in _TEXT_TYPES
        or kind.endswith(("+json", "+xml"))
        or (kind in _GENERIC and ext in _TEXT_EXT)
    ):
        return data.decode("utf-8", errors="replace")[:TEXT_CAP] or None
    return None
