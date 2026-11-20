"""Blobs: raw bytes named by their sha256, referenced from documents by `BlobRef`."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Final

from pydantic import JsonValue

SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_REF_KEYS: Final = frozenset({"sha256", "size", "media_type", "name"})
BLOB_DIR: Final = "blobs/sha256/"


def blob_path(sha256: str) -> str:
    return f"{BLOB_DIR}{sha256}"


def _is_ref(node: dict[str, JsonValue]) -> bool:
    sha = node.get("sha256")
    return (
        isinstance(sha, str)
        and bool(SHA256.match(sha))
        and isinstance(node.get("size"), int)
        and isinstance(node.get("media_type"), str)
        and node.keys() <= _REF_KEYS
    )


def ref_nodes(document: JsonValue) -> Iterator[dict[str, JsonValue]]:
    """Every `BlobRef` object anywhere in `document`, as the dict it is (edits show in place)."""
    stack: list[JsonValue] = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if _is_ref(node):
                yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def blob_refs(document: JsonValue) -> set[str]:
    """The sha256 of every `BlobRef` anywhere in `document`."""
    return {str(ref["sha256"]) for ref in ref_nodes(document)}
