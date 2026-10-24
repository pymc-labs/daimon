"""Which Microsoft Graph v1.0 paths the Microsoft tools may call.

A path is relative to the v1.0 root and must match one of a few shapes under
OneDrive, SharePoint sites, mail, calendar and OneNote. Anything else, any
absolute URL, dot segment, encoded separator or unlisted query key is refused
before a request is made. Checks run on the percent-decoded path, since Graph
decodes it too; the path is sent as given. Mail, calendar and any OneNote path are personal:
callers show them only in the person's 1:1 chat. Sharing permissions and
thumbnails carry pre-authenticated links, so they are refused outright.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote

from daimon.core.microsoft_write_paths import WriteKind, write_kind
from fastmcp.exceptions import ToolError

_FILES = frozenset({"drive", "drives", "sites"})
_PERSONAL = frozenset(
    {"messages", "mailfolders", "events", "calendarview", "calendar", "calendars", "onenote"}
)
_QUERY_KEYS = frozenset(
    {
        "$select",
        "$top",
        "$skip",
        "$filter",
        "$orderby",
        "$search",
        "$count",
        "$expand",
        "$skiptoken",
        "startdatetime",
        "enddatetime",
        "search",
    }
)
# Their answers hold links anyone can open without signing in.
_BEARER = frozenset({"permissions", "thumbnails"})
_ESCAPES = re.compile(r"(?:%[0-9A-Fa-f]{2})+")


@dataclass(frozen=True, slots=True)
class GraphPath:
    path: str  # as given, still percent-encoded; what is sent
    decoded: str  # what every check ran on
    query: tuple[tuple[str, str], ...]
    personal: bool


def _refuse(why: str) -> ToolError:
    return ToolError(
        f"Refused Graph path: {why}. Use a path relative to the v1.0 root under /me/drive, "
        "/drives, /sites, /me/messages, /me/mailFolders, /me/events, /me/calendarView or "
        "/me/onenote."
    )


def _decode(path: str) -> str:
    """`path` with escapes decoded; refused when one hides a separator or control."""

    def decode_run(match: re.Match[str]) -> str:
        text = unquote(match.group(), errors="strict")
        if any(ch in "/\\?#%" or ord(ch) < 32 or 127 <= ord(ch) < 160 for ch in text):
            raise _refuse("it encodes a slash, backslash, query, fragment, percent or control")
        return text

    try:
        decoded = _ESCAPES.sub(decode_run, path)
    except UnicodeDecodeError as err:
        raise _refuse("it contains an escape that is not UTF-8") from err
    if "%" in _ESCAPES.sub("", path):
        raise _refuse("it contains a percent sign that is not an escape")
    return decoded


def parse_path(raw: str) -> GraphPath:
    """Split and check a path any tool may read; raise `ToolError` when refused."""
    if "://" in raw or raw.startswith("//") or not raw.startswith("/"):
        raise _refuse("only a path such as /me/drive/root/children is accepted, not a URL")
    if any(ch in raw for ch in "\\#") or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise _refuse("it contains a backslash, fragment or control character")
    # Only the path is decoded here: parse_qsl decodes query values, checked by key below.
    path, _, query = raw.partition("?")
    if path.lower().startswith("/v1.0/"):
        path = path[len("/v1.0") :]
    decoded = _decode(path)
    segments = decoded.split("/")[1:]
    if any(segment.strip(":") in ("", ".", "..") for segment in segments):
        raise _refuse("it has an empty or dot segment")
    pairs = tuple(parse_qsl(query, keep_blank_values=True))
    if any(key.lower() not in _QUERY_KEYS for key, _ in pairs):
        raise _refuse("it has a query parameter outside $select, $top, $filter and the like")
    lowered = [segment.lower() for segment in segments]
    if _BEARER & set(lowered) or any(
        word in value.lower()
        for key, value in pairs
        if key.lower() in ("$expand", "$select")
        for word in _BEARER
    ):
        raise _refuse("sharing permissions and thumbnails hold pre-authenticated links")
    # A site's notebooks are as personal as the person's own (/sites/{id}/onenote).
    onenote = "onenote" in lowered
    head = lowered[:2]
    if head[0] in ("drives", "sites"):
        return GraphPath(path=path, decoded=decoded, query=pairs, personal=onenote)
    if head[0] == "me" and len(head) == 2 and head[1] in _FILES:
        return GraphPath(path=path, decoded=decoded, query=pairs, personal=onenote)
    if head[0] == "me" and len(head) == 2 and head[1] in _PERSONAL:
        return GraphPath(path=path, decoded=decoded, query=pairs, personal=True)
    raise _refuse("it is outside the files, mail, calendar and notes this tool reaches")


def check_write(method: str, raw: str) -> tuple[GraphPath, WriteKind]:
    """A path `microsoft_write` may change with `method`; raise `ToolError` when refused."""
    target = parse_path(raw)
    if target.query:
        raise _refuse("a write takes no query parameters")
    kind = write_kind(method, target.decoded)
    if kind is not None:
        return target, kind
    raise ToolError(
        f"Refused: {method} {target.path} is not a write this tool makes. It uploads or "
        "replaces a file (PUT .../content), edits an Excel workbook under a drive item "
        "(POST or PATCH .../workbook/...), creates a OneNote page (POST "
        "/me/onenote/sections/{id}/pages) or updates one (PATCH /me/onenote/pages/{id}/content)."
    )
