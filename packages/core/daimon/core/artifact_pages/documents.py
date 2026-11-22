"""A page's shared data: JSON documents in named collections, Firestore-shaped.

Collections starting with `_` are Daimon's own (`_events`): a page cannot reach them,
and tools may read and delete there but never write.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from typing import Final, Literal

from daimon.core.artifact_pages.model import ArtifactError, has_json_nul
from daimon.core.config import ArtifactPagesSettings
from daimon.core.stores.domain import ArtifactUsage, CollectionRule
from pydantic import BaseModel, ConfigDict, JsonValue

_COLLECTION: Final = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_DOC_ID: Final = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_FIELD: Final = re.compile(r"[A-Za-z0-9_]{1,64}\Z")
SERVER_PREFIX: Final = "_"
EVENTS_COLLECTION: Final = "_events"
MAX_RULES: Final = 50
ANYONE: Final = CollectionRule()


class DocumentWrite(BaseModel):
    """`set` replaces or creates; `merge` replaces top-level fields of what is there;
    `update` merges into a document that must exist."""

    model_config = ConfigDict(frozen=True)

    mode: Literal["set", "merge", "update"] = "set"
    data: dict[str, JsonValue]


class ListQuery(BaseModel):
    """One collection, ordered by one top-level field (documents without it last), else
    by creation time if `by_creation`, else by id; at most `limit`. `after` is the last
    id of the previous page, for id order only. `created_by` keeps only that account's."""

    model_config = ConfigDict(frozen=True)

    collection: str
    order_by: str | None = None
    descending: bool = False
    by_creation: bool = False
    limit: int
    after: str | None = None
    created_by: uuid.UUID | None = None


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


def measure(data: JsonValue, limit: int) -> int:
    """A document's stored size; refuses one over `limit` bytes or holding a NUL."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if has_json_nul(text):
        raise ArtifactError("invalid", "Documents cannot hold the NUL character (\\u0000).")
    size = len(text.encode())
    if size > limit:
        raise ArtifactError("too_large", f"The document is {size:,} bytes; the limit is {limit:,}.")
    return size


def check_capacity(
    usage: ArtifactUsage,
    *,
    new_size: int,
    replaced_size: int | None,
    scale: int,
    limits: ArtifactPagesSettings,
) -> None:
    """`replaced_size` is the stored size of the document being overwritten, if any."""
    documents = usage.documents + (1 if replaced_size is None else 0)
    size = usage.bytes - (replaced_size or 0) + new_size
    most, most_bytes = limits.documents * scale, limits.data_bytes * scale
    if documents > most:
        raise ArtifactError(
            "too_large", f"The page holds at most {most:,} documents; delete some first."
        )
    if size > most_bytes:
        raise ArtifactError(
            "too_large", f"The page's data would be {size:,} bytes; the limit is {most_bytes:,}."
        )


def build_list_query(
    collection: str,
    *,
    order_by: str | None = None,
    descending: bool = False,
    limit: int | None = None,
    after: str | None = None,
    by_creation: bool = False,
    limits: ArtifactPagesSettings,
) -> ListQuery:
    if order_by is not None and _FIELD.match(order_by) is None:
        raise ArtifactError("invalid", f"orderBy takes one top-level field name, not {order_by!r}.")
    most = limits.list_documents
    size = most if limit is None else limit
    if not 1 <= size <= most:
        raise ArtifactError("invalid", f"limit must be 1-{most}.")
    if after is not None and (order_by is not None or by_creation):
        raise ArtifactError("invalid", "A cursor pages through id order only.")
    return ListQuery(
        collection=check_collection(collection),
        order_by=order_by,
        descending=descending,
        by_creation=by_creation,
        limit=size,
        after=None if after is None else check_doc_id(after),
    )


def check_list_size(size: int, limits: ArtifactPagesSettings) -> None:
    """Bounds one list reply, which every `onSnapshot` refetches on each change."""
    if size > limits.list_bytes:
        raise ArtifactError(
            "too_large",
            f"This list is {size:,} bytes; the limit is {limits.list_bytes:,}. "
            "Use a smaller limit.",
        )


def check_rules(rules: Mapping[str, CollectionRule]) -> None:
    if len(rules) > MAX_RULES:
        raise ArtifactError("invalid", f"At most {MAX_RULES} collection rules per page.")
    for name in rules:
        check_page_collection(name)


def rule_of(rules: Mapping[str, CollectionRule], collection: str) -> CollectionRule:
    return rules.get(collection, ANYONE)


def may_read(rule: CollectionRule, created_by: uuid.UUID | None, viewer: uuid.UUID | None) -> bool:
    """Whether a viewer may read a document; an anonymous one has none of their own."""
    return rule.read == "all" or (viewer is not None and created_by == viewer)


def check_write(
    rule: CollectionRule, *, exists: bool, created_by: uuid.UUID | None, viewer: uuid.UUID | None
) -> None:
    """A viewer's set, add or delete under the collection's rule; refused as `not_allowed`."""
    if rule.write == "all":
        return
    if rule.write == "none":
        raise ArtifactError("not_allowed", "This collection is read-only for viewers.")
    if viewer is None or (exists and created_by != viewer):
        raise ArtifactError("not_allowed", "Viewers can change only their own documents here.")
