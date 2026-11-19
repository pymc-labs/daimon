"""Digest bodies that stay stable when a contract gains a field.

A field declared with `additive()` is left out of `digest_body` while it holds
its default, so adding one keeps every stored revision's and plan's digest.
Untagged fields are always included, exactly as `model_dump(mode="json")` has them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from pydantic import BaseModel, Field, JsonValue
from pydantic.fields import FieldInfo

__all__ = ["ADDITIVE", "additive", "digest_body", "is_additive"]

ADDITIVE = "additive"


def additive[T](default: T) -> T:
    """A field added after digests were stored: absent from them while at `default`."""
    return Field(default=default, json_schema_extra={ADDITIVE: True})


def is_additive(info: FieldInfo) -> bool:
    """Whether the field was declared with `additive()`."""
    extra = info.json_schema_extra
    return isinstance(extra, dict) and cast(dict[str, object], extra).get(ADDITIVE) is True


def digest_body(model: BaseModel, *, include: set[str] | None = None) -> dict[str, JsonValue]:
    """`model` as JSON for hashing, without additive fields at their default."""
    body = _prune(model, model.model_dump(mode="json", include=include))
    assert isinstance(body, dict)
    return body


def _prune(value: object, body: JsonValue) -> JsonValue:
    if isinstance(value, BaseModel) and isinstance(body, dict):
        for name, info in type(value).model_fields.items():
            if name not in body:
                continue
            current: object = getattr(value, name)
            if is_additive(info) and current == info.get_default(call_default_factory=True):
                del body[name]
            else:
                body[name] = _prune(current, body[name])
    elif isinstance(value, list | tuple) and isinstance(body, list):
        items = cast(Sequence[object], value)
        body = [_prune(item, dumped) for item, dumped in zip(items, body, strict=True)]
    elif isinstance(value, Mapping) and isinstance(body, dict):
        values = cast(Mapping[object, object], value).values()
        body = {
            key: _prune(item, dumped)
            for item, (key, dumped) in zip(values, body.items(), strict=True)
        }
    return body
