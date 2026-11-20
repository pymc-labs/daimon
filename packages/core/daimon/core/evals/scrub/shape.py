"""Which fields of a bundle document hold text to scrub, read from the format models.

A shape is `KEEP` (an enum, number, time or media type: written by Daimon, kept as it is),
`None` (free JSON: scrubbed) or a dict of field shapes, where `"*"` stands for any key.
"""

from __future__ import annotations

import types
from datetime import date
from typing import Final, Literal, Union, get_args, get_origin

from daimon.core.evals import format as fmt
from pydantic import BaseModel, JsonValue

KEEP: Final = "keep"
Shape = dict[str, "Shape"] | str | None
# Text fields Daimon writes: media types, hashes, tool names, effect verbs, HTTP methods.
_KEPT_NAMES: Final = frozenset({"media_type", "sha256", "hash", "policy", "tool", "verb", "method"})
_BUILT: dict[type[BaseModel], dict[str, Shape]] = {}


def _merge(a: Shape, b: Shape) -> Shape:
    if a is None or b is None:
        return None
    if not isinstance(a, dict):
        return b
    if not isinstance(b, dict) or a is b:
        return a
    return {k: _merge(a[k], b[k]) if k in a and k in b else a.get(k, b.get(k)) for k in a | b}


def _of(tp: object) -> Shape:
    origin = get_origin(tp)
    if origin is Literal or (isinstance(tp, type) and issubclass(tp, bool | int | float | date)):
        return KEEP
    if origin in (Union, types.UnionType):
        parts = [_of(a) for a in get_args(tp) if a is not type(None)]
        out = parts[0]
        for part in parts[1:]:
            out = _merge(out, part)
        return out
    if origin in (list, tuple, set, frozenset):
        return _of(get_args(tp)[0])
    if origin is dict:
        return {"*": _of(get_args(tp)[1])}
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return _model(tp)
    return None


def _model(model: type[BaseModel]) -> dict[str, Shape]:
    if (built := _BUILT.get(model)) is not None:
        return built
    out: dict[str, Shape] = {}
    _BUILT[model] = out
    for name, field in model.model_fields.items():
        key = field.alias or name
        shape = _of(field.annotation)
        named = key in _KEPT_NAMES and str in (field.annotation, *get_args(field.annotation))
        out[key] = KEEP if named and shape is None else shape
    return out


_ROOTS: Final[dict[str, tuple[type[BaseModel], ...]]] = {
    fmt.MANIFEST_SCHEMA: (fmt.Manifest,),
    fmt.SUITE_SCHEMA: (fmt.Suite,),
    fmt.CASE_SCHEMA: (fmt.Case,),
    fmt.RUN_SCHEMA: (fmt.Run,),
    fmt.ITEM_SCHEMA: (fmt.RunItem,),
    fmt.SCORE_SCHEMA: (fmt.Score,),
    fmt.REVIEW_SCHEMA: (fmt.ReviewQueue, fmt.Comment),
}


def shape_of(document: JsonValue) -> Shape:
    """The shape of a bundle document, by its `schema`; None for anything else."""
    schema = document.get("schema") if isinstance(document, dict) else None
    models = _ROOTS.get(schema) if isinstance(schema, str) else None
    out: Shape = None
    for n, model in enumerate(models or ()):
        out = _model(model) if n == 0 else _merge(out, _model(model))
    return out


def field_shape(shape: Shape, key: str) -> Shape:
    """The shape of `key` inside an object of `shape`."""
    return shape.get(key, shape.get("*")) if isinstance(shape, dict) else None
