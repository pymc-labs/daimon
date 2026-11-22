"""A page's shared data: JSON documents in named collections, Firestore-shaped.

Collections starting with `_` are Daimon's own (`_events`): a page cannot reach them,
and tools may read and delete there but never write.
"""

from __future__ import annotations

import json
import re
from typing import Final, Literal

from daimon.core.artifact_pages.model import ArtifactError, Limits, has_json_nul
from daimon.core.stores.domain import ArtifactUsage
from pydantic import BaseModel, ConfigDict, JsonValue

_COLLECTION: Final = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_DOC_ID: Final = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_FIELD: Final = re.compile(r"[A-Za-z0-9_]{1,64}\Z")
SERVER_PREFIX: Final = "_"
EVENTS_COLLECTION: Final = "_events"


class DocumentWrite(BaseModel):
    """`set` replaces or creates; `merge` replaces top-level fields of what is there;
    `update` merges into a document that must exist."""

    model_config = ConfigDict(frozen=True)

    mode: Literal["set", "merge", "update"] = "set"
    data: dict[str, JsonValue]


class ListQuery(BaseModel):
    """One collection, ordered by one top-level field (else by id), at most `limit`.
    `after` is the last id of the previous page, for id order only."""

    model_config = ConfigDict(frozen=True)

    collection: str
    order_by: str | None = None
    descending: bool = False
    limit: int = Limits.LIST_DOCUMENTS
    after: str | None = None


def check_collection(name: str) -> str:
    if _COLLECTION.match(name) is None:
        raise ArtifactError("invalid", f"Invalid collection {name!r}: 1-64 of A-Z a-z 0-9 _ -.")
    return name


def check_doc_id(doc_id: str) -> str:
    if _DOC_ID.match(doc_id) is None:
        raise ArtifactError(
            "invalid", f"Invalid document id {doc_id!r}: 1-128 of A-Z a-z 0-9 _ . : -."
        )
    return doc_id


def check_page_collection(name: str) -> str:
    """What a page's own code may read: none of Daimon's collections."""
    if check_collection(name).startswith(SERVER_PREFIX):
        raise ArtifactError(
            "forbidden", f"{name} is Daimon's; a page's collections cannot start with _."
        )
    return name


def check_writable_collection(name: str) -> str:
    """What anyone but the server may write: none of Daimon's collections."""
    if check_collection(name).startswith(SERVER_PREFIX):
        raise ArtifactError("forbidden", f"{name} is written by Daimon only.")
    return name


def apply_write(
    write: DocumentWrite, existing: dict[str, JsonValue] | None
) -> dict[str, JsonValue]:
    """The document after the write, as Firestore does it."""
    if write.mode == "set":
        return write.data
    if existing is None and write.mode == "update":
        raise ArtifactError("not_found", "No such document to update; use set to create it.")
    return {**(existing or {}), **write.data}


def measure(data: JsonValue) -> int:
    """A document's stored size; refuses one over the limit or holding a NUL."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if has_json_nul(text):
        raise ArtifactError("invalid", "Documents cannot hold the NUL character (\\u0000).")
    size = len(text.encode())
    if size > Limits.DOCUMENT_BYTES:
        raise ArtifactError(
            "too_large", f"The document is {size:,} bytes; the limit is {Limits.DOCUMENT_BYTES:,}."
        )
    return size


def max_documents(scale: int) -> int:
    return Limits.DOCUMENTS * scale


def max_bytes(scale: int) -> int:
    return Limits.DATA_BYTES * scale


def check_capacity(
    usage: ArtifactUsage, *, new_size: int, replaced_size: int | None, scale: int
) -> None:
    """`replaced_size` is the stored size of the document being overwritten, if any."""
    documents = usage.documents + (1 if replaced_size is None else 0)
    size = usage.bytes - (replaced_size or 0) + new_size
    if documents > max_documents(scale):
        raise ArtifactError(
            "too_large",
            f"The page holds at most {max_documents(scale):,} documents; delete some first.",
        )
    if size > max_bytes(scale):
        raise ArtifactError(
            "too_large",
            f"The page's data would be {size:,} bytes; the limit is {max_bytes(scale):,}.",
        )


def build_list_query(
    collection: str,
    *,
    order_by: str | None = None,
    descending: bool = False,
    limit: int | None = None,
    after: str | None = None,
) -> ListQuery:
    if order_by is not None and _FIELD.match(order_by) is None:
        raise ArtifactError("invalid", f"orderBy takes one top-level field name, not {order_by!r}.")
    size = Limits.LIST_DOCUMENTS if limit is None else limit
    if not 1 <= size <= Limits.LIST_DOCUMENTS:
        raise ArtifactError("invalid", f"limit must be 1-{Limits.LIST_DOCUMENTS}.")
    if after is not None and order_by is not None:
        raise ArtifactError("invalid", "A cursor pages through id order only.")
    return ListQuery(
        collection=check_collection(collection),
        order_by=order_by,
        descending=descending,
        limit=size,
        after=None if after is None else check_doc_id(after),
    )


def check_list_size(size: int) -> None:
    """Bounds one list reply, which every `onSnapshot` refetches on each change."""
    if size > Limits.LIST_BYTES:
        raise ArtifactError(
            "too_large",
            f"This list is {size:,} bytes; the limit is {Limits.LIST_BYTES:,}. "
            "Use a smaller limit.",
        )
