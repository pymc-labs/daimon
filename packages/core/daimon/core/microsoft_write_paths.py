"""Which Graph paths `microsoft_write` changes, and how.

The MCP tool checks a write against these shapes before it runs, and the
approval card names the change from the same table, so a folder called
"OneNote" can't make a file upload read as a notebook edit. Paths are the
percent-decoded form the tool checks.
"""

from __future__ import annotations

import re
from typing import Final, Literal

__all__ = ["WriteKind", "write_kind"]

WriteKind = Literal["upload", "workbook", "onenote_create", "onenote_update"]

_DRIVE = r"/(?:me/drive|drives/[^/:]+|sites/[^/:]+/drive)"
_ITEM = _DRIVE + r"/(?:root|items/[^/:]+)(?::/[^:]+:)?"
_WRITES: Final[tuple[tuple[frozenset[str], re.Pattern[str], WriteKind], ...]] = (
    (frozenset({"PUT"}), re.compile(_ITEM + r"/content", re.I), "upload"),
    # A colon only inside a quoted argument (range(address='A1:B2')), never
    # path addressing that could step out of the workbook.
    (
        frozenset({"POST", "PATCH"}),
        re.compile(_ITEM + r"/workbook/(?:[^:']|'[^'/]*')+", re.I),
        "workbook",
    ),
    (
        frozenset({"POST"}),
        re.compile(r"/me/onenote/(?:sections/[^/:]+/)?pages", re.I),
        "onenote_create",
    ),
    (frozenset({"PATCH"}), re.compile(r"/me/onenote/pages/[^/:]+/content", re.I), "onenote_update"),
)


def write_kind(method: str, decoded_path: str) -> WriteKind | None:
    """The kind of write `method` makes on `decoded_path`, or None when it is not one."""
    for methods, pattern, kind in _WRITES:
        if method in methods and pattern.fullmatch(decoded_path):
            return kind
    return None
