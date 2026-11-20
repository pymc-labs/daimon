"""Which fields of a bundle document hold text to scrub, read from the bundle's JSON schema.

A shape is `KEEP` (an enum, number, time, hash or media type: written by Daimon, kept as it
is), `ID` (an id: faked the same everywhere, in a form the id pattern accepts), `None` (free
JSON: scrubbed) or a dict of field shapes, where `"*"` stands for any key.
"""

from __future__ import annotations

from typing import Final

from daimon.core.evals import format as fmt
from daimon.core.evals.validate import root_schema
from pydantic import JsonValue

KEEP: Final = "keep"
ID: Final = "id"
Shape = dict[str, "Shape"] | str | None
# Text fields Daimon writes: media types, hashes, tool names, effect verbs, HTTP methods.
_KEPT_NAMES: Final = frozenset({"media_type", "sha256", "hash", "policy", "tool", "verb", "method"})
# A pointer names fields: it is scrubbed like text. Every other pattern fixes a format, as
# `date-time` does (a `uri` is scrubbed).
_FREE_DEFS: Final = frozenset({"JsonPointer"})
_TYPED: Final = frozenset(
    {"$ref", "type", "properties", "items", "enum", "const", "format", "pattern", "oneOf",
     "anyOf", "allOf", "additionalProperties"}
)  # fmt: skip
_BUILT: dict[str, Shape] = {}


def _merge(a: Shape, b: Shape) -> Shape:
    """A shape for values of either: a free or object shape wins over `ID`, which wins over
    `KEEP`, since a value of the wider one must be scrubbed."""
    if a is None or b is None:
        return None
    if isinstance(a, dict) and isinstance(b, dict):
        if a is b:
            return a
        return {k: _merge(a[k], b[k]) if k in a and k in b else a.get(k, b.get(k)) for k in a | b}
    if isinstance(a, dict) or isinstance(b, dict):
        return a if isinstance(a, dict) else b
    return ID if ID in (a, b) else KEEP


def _of(node: JsonValue) -> Shape:
    if not isinstance(node, dict):
        return None
    if isinstance(ref := node.get("$ref"), str):
        return _def(ref.rsplit("/", 1)[-1])
    if "const" in node or "enum" in node or "pattern" in node or node.get("format") == "date-time":
        return KEEP
    kinds = node.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    if kinds and all(k in ("boolean", "integer", "number", "null") for k in kinds):
        return KEEP
    # Alternatives that only add `required` say nothing about values.
    parts = [
        _of(p) for k in ("oneOf", "anyOf", "allOf") for p in _list(node.get(k))
        if isinstance(p, dict) and p.keys() & _TYPED
    ]  # fmt: skip
    props = node.get("properties")
    if "items" in node:
        parts.append(_of(node["items"]))
    elif isinstance(props, dict) or "additionalProperties" in node or "object" in kinds:
        own: dict[str, Shape] = {}
        for key, field in (props if isinstance(props, dict) else {}).items():
            shape = _of(field)
            own[key] = KEEP if key in _KEPT_NAMES and shape is None else shape
        extra = node.get("additionalProperties", True)
        if extra is not False:
            own["*"] = _of(extra) if isinstance(extra, dict) else None
        parts.append(own)
    if not parts:
        return None
    out = parts[0]
    for part in parts[1:]:
        out = _merge(out, part)
    return out


def _list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _def(name: str) -> Shape:
    """The shape of `$defs/name`. An object's dict is cached before it is filled, so a
    definition that refers to itself ends."""
    if name in _BUILT:
        return _BUILT[name]
    if name == "Id":
        return ID
    node = _defs().get(name)
    if name in _FREE_DEFS:
        return None
    placeholder: dict[str, Shape] = {}
    _BUILT[name] = placeholder
    shape = _of(node)
    if isinstance(shape, dict):
        placeholder.update(shape)
        shape = placeholder
    _BUILT[name] = shape
    return shape


def _defs() -> dict[str, JsonValue]:
    defs = root_schema().get("$defs")
    return defs if isinstance(defs, dict) else {}


_ROOTS: Final[dict[str, str]] = {
    fmt.MANIFEST_SCHEMA: "Manifest",
    fmt.SUITE_SCHEMA: "Suite",
    fmt.CASE_SCHEMA: "Case",
    fmt.RUN_SCHEMA: "Run",
    fmt.ITEM_SCHEMA: "RunItem",
    fmt.SCORE_SCHEMA: "Score",
    fmt.REVIEW_SCHEMA: "ReviewRecord",
}


def shape_of(document: JsonValue) -> Shape:
    """The shape of a bundle document, by its `schema`; None for anything else."""
    schema = document.get("schema") if isinstance(document, dict) else None
    root = _ROOTS.get(schema) if isinstance(schema, str) else None
    return _def(root) if root else None


def field_shape(shape: Shape, key: str) -> Shape:
    """The shape of `key` inside an object of `shape`."""
    return shape.get(key, shape.get("*")) if isinstance(shape, dict) else None
