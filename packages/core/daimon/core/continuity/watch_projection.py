"""Bounded, deterministic watch projections and comparison rules.

This deliberately small jq-style subset accepts identity, object-field paths,
numeric array indexes and, for polls, one `[]` map step after a field or index
(`.results[].id`). It has no evaluation, subprocess, network, regex, recursion
or other expansion operators. A migrated jq expression outside this subset
requires an operator rewrite before enrollment; never silently ignore it.

I7 (bounds) has three tiers. A raw source document is checked against
`SOURCE` before projection and is never kept. What is kept is checked again:
a baseline against `BASELINE` (keyed `added` keeps at most `MAX_KEYS` key
digests, never the keys), each evidence item is clipped to `ITEM_BYTES`, and
one wake payload holds at most `MAX_ITEMS` items in `PAYLOAD_BYTES`, counting
the rest as `omitted`. The firing SQL enforces the baseline and payload bounds
again. Arguments and page-event documents keep the original `DEFAULT` bounds.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import JsonValue

MAX_BYTES = 4096
MAX_DEPTH = 16
MAX_NODES = 1024
MAX_PATH = 256
Rule = Literal["change", "added", "updated"]
_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[([0-9]{1,4})\]|(\[\])")


@dataclass(frozen=True)
class Limits:
    bytes: int
    depth: int
    nodes: int


DEFAULT: Final = Limits(MAX_BYTES, MAX_DEPTH, MAX_NODES)
SOURCE: Final = Limits(262144, 32, 20000)
BASELINE: Final = Limits(65536, 32, 20000)
ITEM_BYTES: Final = 1024
PAYLOAD_BYTES: Final = 16384
MAX_ITEMS: Final = 20
MAX_KEYS: Final = 2000
MAX_EVIDENCE_PATHS: Final = 8
DIGEST_CHARS: Final = 24


class _Map:
    """The single `[]` step: apply the rest of the path to every array element."""

    def __repr__(self) -> str:
        return "[]"


MAP: Final = _Map()
Step = str | int | _Map


def bounded(value: JsonValue, limits: Limits = DEFAULT) -> JsonValue:
    """Validate both source and output before traversal or serialization.

    Iterative traversal bounds work even for adversarial in-memory trees.
    Shared subtrees count for every visit; cycles exhaust the depth bound.
    Non-JSON values and non-finite numbers fail closed.
    """
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > limits.depth or nodes > limits.nodes:
            raise ValueError("Watch document exceeds depth or node limit")
        if isinstance(item, dict):
            if len(item) + nodes + len(pending) > limits.nodes or any(
                not isinstance(key, str) or len(key) > limits.bytes  # pyright: ignore[reportUnnecessaryIsInstance]
                for key in item
            ):
                raise ValueError("Watch object exceeds limits")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if len(item) + nodes + len(pending) > limits.nodes:
                raise ValueError("Watch array exceeds limits")
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > limits.bytes:
                raise ValueError("Watch string exceeds limit")
        elif item is not None and type(item) not in (bool, int, float):
            raise ValueError("Watch document must be JSON")
        elif type(item) is int and item.bit_length() > 128:
            raise ValueError("Watch integer exceeds limit")
    if len(_text(value)) > limits.bytes:
        raise ValueError("Watch document exceeds byte limit")
    return value


def _text(value: JsonValue) -> str:
    # ASCII escapes make the character count an upper bound on stored bytes.
    return json.dumps(value, allow_nan=False, ensure_ascii=True)


def projection_path(expression: str, *, allow_map: bool = False) -> list[Step]:
    """Parse `.`, `.field.nested`, `.rows[0].status` and, with `allow_map`, `.rows[].id`."""
    if expression == ".":
        return []
    if not expression or len(expression) > MAX_PATH:
        raise ValueError("Unsupported watch projection")
    position = 0
    path: list[Step] = []
    while position < len(expression):
        match = _TOKEN.match(expression, position)
        if match is None:
            raise ValueError("Unsupported watch projection")
        field, index, each = match.groups()
        if each is not None:
            # One map step, never first: `.[]` and nested maps stay refused.
            if not allow_map or not path or MAP in path:
                raise ValueError("Unsupported watch projection")
            path.append(MAP)
        else:
            path.append(field if field is not None else int(index))
        position = match.end()
        if len(path) > MAX_DEPTH:
            raise ValueError("Watch projection is too deep")
    return path


def _walk(current: JsonValue, path: Sequence[Step]) -> JsonValue:
    for position, part in enumerate(path):
        if isinstance(part, _Map):
            if not isinstance(current, list):
                return None
            rest = path[position + 1 :]
            return [_walk(item, rest) for item in current]
        if isinstance(part, str) and isinstance(current, dict):
            current = current.get(part)
        elif isinstance(part, int) and isinstance(current, list):
            current = current[part] if part < len(current) else None
        else:
            current = None
    return current


def project(
    document: JsonValue,
    expression: str = ".",
    *,
    limits: Limits = DEFAULT,
    allow_map: bool = False,
) -> JsonValue:
    current = bounded(document, limits)
    return bounded(_walk(current, projection_path(expression, allow_map=allow_map)), limits)


def _key(value: JsonValue) -> str:
    # JSON equality preserves boolean/number distinctions, unlike Python ==.
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def evaluate(
    rule: Rule, previous: JsonValue, current: JsonValue, *, limits: Limits = DEFAULT
) -> JsonValue:
    """Return firing evidence or None; caller always saves a successful baseline.

    `change` suppresses empty/false results, but zero is meaningful. `added`
    compares full array elements; `updated` compares objects by stable key.
    Deletions alone do not fire. Initial-baseline handling belongs to the
    transaction caller and must distinguish uninitialized from JSON null.
    """
    bounded(previous, limits)
    bounded(current, limits)
    if rule == "change":
        meaningful = current is not None and current is not False and current not in ("", [], {})
        return current if meaningful and _key(previous) != _key(current) else None
    if rule == "added":
        if not isinstance(previous, list) or not isinstance(current, list):
            raise ValueError("added requires array baselines")
        seen = {_key(item) for item in previous}
        result: list[JsonValue] = []
        for item in current:
            key = _key(item)
            if key not in seen:
                result.append(item)
                seen.add(key)
        return bounded(result, limits) if result else None
    if rule == "updated":
        if not isinstance(previous, dict) or not isinstance(current, dict):
            raise ValueError("updated requires object baselines")
        changed = {
            key: value
            for key, value in current.items()
            if key not in previous or _key(previous[key]) != _key(value)
        }
        return bounded(changed, limits) if changed else None
    raise ValueError("Unknown watch comparison rule")


def split_key(key: str) -> tuple[list[Step], list[Step]]:
    """`.rows[].id` names the array (`.rows`) and each element's key (`.id`)."""
    path = projection_path(key, allow_map=True)
    if MAP not in path:
        raise ValueError("A watch key needs one [] step, such as .rows[].id")
    split = path.index(MAP)
    return path[:split], path[split + 1 :]


def evidence_paths(paths: Sequence[str]) -> list[list[Step]]:
    if len(paths) > MAX_EVIDENCE_PATHS:
        raise ValueError(f"A watch keeps at most {MAX_EVIDENCE_PATHS} evidence fields")
    return [projection_path(path) for path in paths]


def validate_rule(rule: str, *, key: str | None, evidence: Sequence[str]) -> None:
    if (key is not None or evidence) and rule != "added":
        raise ValueError("A key and evidence fields only work with the added rule")
    if key is not None:
        split_key(key)
    evidence_paths(evidence)


def _digest(value: JsonValue) -> str:
    return hashlib.sha256(_key(value).encode()).hexdigest()[:DIGEST_CHARS]


def _keyed(projected: JsonValue, key: str) -> list[tuple[str, JsonValue]]:
    array_path, key_path = split_key(key)
    elements = _walk(projected, array_path)
    if not isinstance(elements, list):
        raise ValueError("added requires array baselines")
    result: list[tuple[str, JsonValue]] = []
    seen: set[str] = set()
    for element in elements:
        identity = _walk(element, key_path)
        if identity is None:
            continue  # An element without a key cannot be tracked.
        digest = _digest(identity)
        if digest not in seen:
            seen.add(digest)
            result.append((digest, element))
    if len(result) > MAX_KEYS:
        raise ValueError(f"A keyed watch tracks at most {MAX_KEYS} items")
    return result


def kept(projected: JsonValue, *, rule: str, key: str | None) -> JsonValue:
    """The value a baseline keeps: key digests for keyed `added`, else the projection."""
    if rule == "added" and key is not None:
        return bounded({"keys": [digest for digest, _ in _keyed(projected, key)]}, BASELINE)
    return bounded(projected, BASELINE)


def compare(
    previous: JsonValue,
    projected: JsonValue,
    *,
    rule: Rule,
    key: str | None,
    evidence: Sequence[str] = (),
) -> JsonValue:
    """Firing evidence or None. `previous` is what `kept` saved for the same rule."""
    paths = evidence_paths(evidence)
    new: list[JsonValue]
    if rule == "added" and key is not None:
        keys = previous.get("keys") if isinstance(previous, dict) else None
        if not isinstance(keys, list):
            raise ValueError("added requires array baselines")
        seen = {item for item in keys if isinstance(item, str)}
        new = [element for digest, element in _keyed(projected, key) if digest not in seen]
    else:
        found = evaluate(rule, previous, bounded(projected, BASELINE), limits=BASELINE)
        if not paths or found is None:
            return found
        assert isinstance(found, list)
        new = found
    if not new:
        return None
    if paths:
        return [
            {
                expression: _walk(item, path)
                for expression, path in zip(evidence, paths, strict=True)
            }
            for item in new
        ]
    return new


def _clip(value: JsonValue, limit: int) -> JsonValue:
    text = _text(value)
    if len(text) <= limit:
        return value
    # Escaping can grow the cut text, so find the longest prefix whose wrapper fits.
    low, high = 0, min(len(text), limit)
    while low < high:
        middle = (low + high + 1) // 2
        if len(_text({"truncated": text[:middle]})) <= limit:
            low = middle
        else:
            high = middle - 1
    return {"truncated": text[:low]}


def payload(found: JsonValue, *, items: bool) -> str:
    """One wake payload within PAYLOAD_BYTES; item lists keep MAX_ITEMS and count the rest."""
    if not items:
        text = _text(found)
        if len(text) <= PAYLOAD_BYTES:
            return text
        if not isinstance(found, list):
            return _text(_clip(found, PAYLOAD_BYTES - 64))
    assert isinstance(found, list)
    chosen: list[JsonValue] = []
    size = 64  # Room for the envelope and the omitted count.
    omitted = 0
    for item in found:
        clipped = _clip(item, ITEM_BYTES)
        cost = len(_text(clipped)) + 2
        if len(chosen) < MAX_ITEMS and size + cost <= PAYLOAD_BYTES:
            chosen.append(clipped)
            size += cost
        else:
            omitted += 1
    return _text({"items": chosen, "omitted": omitted})
