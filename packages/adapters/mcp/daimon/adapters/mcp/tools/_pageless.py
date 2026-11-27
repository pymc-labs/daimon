"""The watch tools as listed where pages don't exist or are off: no page source, field or word."""

from __future__ import annotations

import re
from typing import Final

from fastmcp.tools.base import Tool

_TEXT: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "create_watch": (
        (r"secret link, or\s+wake on a page's events\.", "secret link."),
        (r"conversation, wake on", "conversation, or wake on"),
        (
            r'With source "page_event".*?also works in the Daimon app\.',
            "Other sources are offered only where they are set up.",
        ),
        (
            r", read_channel and artifact_db_query \(a\s+page's data, read as the owner\)",
            " and read_channel",
        ),
    ),
    "rotate_watch_key": (
        (r"A page\s+watch's new key is set up by Daimon; any other's", "Its new key"),
    ),
}
_PAGE_FIELDS: Final = ("page", "select", "where")


def without_pages(tool: Tool) -> Tool:
    """`tool` with its page source, fields and sentences taken out; other tools unchanged."""
    edits = _TEXT.get(tool.name)
    if edits is None:
        return tool
    text = tool.description or ""
    for pattern, replacement in edits:
        text = re.sub(pattern, replacement, text, flags=re.DOTALL)
    params = tool.parameters
    if tool.name == "create_watch":
        props = {k: v for k, v in params["properties"].items() if k not in _PAGE_FIELDS}
        props["source"] = {"enum": ["poll", "callback"], "type": "string"}
        params = {**params, "properties": props}
    return tool.model_copy(update={"description": text, "parameters": params})
